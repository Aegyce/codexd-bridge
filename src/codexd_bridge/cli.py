"""Command-line setup and lifecycle operations; session traffic uses MCP."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config import ConfigError, _resolve_config_path, load_backends
from .transport import BridgeError


def registry_path(explicit: Path | None) -> Path:
    return _resolve_config_path(explicit)[0].absolute()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bridge independent Codex daemons across providers"
    )
    result.add_argument("--config", type=Path, help="Bridge backend registry TOML")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("ui", help="Open the interactive provider, model and session manager")
    commands.add_parser("serve", help="Run the MCP stdio server")
    provider = commands.add_parser("provider", help="Configure providers and codex-NAME launchers")
    provider_actions = provider.add_subparsers(dest="provider_action")
    add = provider_actions.add_parser("add", help="Create and verify a new provider from connection settings")
    add.add_argument("name", nargs="?", help="Omit to open the TUI")
    add.add_argument("--base-url")
    add.add_argument("--env-key", help="API key environment variable NAME, not its value")
    add.add_argument("--model")
    add.add_argument("--context-window", type=int, help="Provider's documented context limit in tokens")
    add.add_argument("--effort")
    add.add_argument("--wire-api", choices=("responses", "chat"), default="responses")
    add.add_argument("--model-catalog", type=Path, help="Existing native model catalog; otherwise declare only --model")
    add.add_argument("--home", type=Path, help="New absolute Codex home")
    finish = provider_actions.add_parser("connect", help="Verify an existing route and install its launcher")
    finish.add_argument("name")
    for command in (add, finish):
        command.add_argument("--launcher-dir", type=Path, help="Launcher directory (default: ~/.local/bin)")
        command.add_argument("--executable", type=Path, help="Absolute installed codexd-bridge executable")
        command.add_argument("--timeout", type=float, default=60)
    run = provider_actions.add_parser("run", help="Run the selected provider's native Codex CLI")
    run.add_argument("name")
    run.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to codex")
    daemon = commands.add_parser("daemon", help="Create or manage an independent Codex daemon")
    actions = daemon.add_subparsers(dest="action", required=True)
    create = actions.add_parser(
        "create", help="Create a new configuration home and register its route"
    )
    create.add_argument("name", help="Route alias in the bridge registry")
    create.add_argument(
        "--home", type=Path, required=True, help="New absolute Codex home; must not exist"
    )
    create.add_argument("--provider", required=True, help="Exact model provider ID")
    create.add_argument("--model", required=True)
    create.add_argument("--effort")
    create.add_argument(
        "--template-home", type=Path, help="Copy non-secret connection metadata only"
    )
    actions.add_parser("list", help="List registered routes without starting daemons")
    for action in ("start", "status", "stop"):
        command = actions.add_parser(action, help=f"{action.capitalize()} the selected daemon")
        command.add_argument("name")
        command.add_argument("--timeout", type=float, default=45)
    connect = commands.add_parser(
        "connect", help="Install discovery skill and register MCP on a running daemon"
    )
    connect.add_argument("name")
    connect.add_argument(
        "--executable", type=Path, help="Absolute installed codexd-bridge executable"
    )
    connect.add_argument(
        "--update-skill",
        action="store_true",
        help="Replace the installed bridge skill, including local edits, with the bundled version",
    )
    connect.add_argument(
        "--allow-session-writes",
        action="store_true",
        help="Explicitly approve the four bridge session mutation tools for unattended delegation",
    )
    models = commands.add_parser(
        "models", help="Discover models dynamically or verify real inference"
    )
    model_actions = models.add_subparsers(dest="model_action", required=True)
    listing = model_actions.add_parser("list", help="Query model/list on configured daemons")
    listing.add_argument("--backend")
    listing.add_argument("--cursor")
    listing.add_argument("--limit", type=int, default=20)
    check = model_actions.add_parser(
        "check", help="Run one short inference and delete its test session"
    )
    check.add_argument("name")
    check.add_argument("--model")
    check.add_argument("--effort")
    check.add_argument("--timeout", type=float, default=60)
    return result


def main(argv: list[str] | None = None) -> None:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        config = registry_path(args.config)
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if (args.command == "ui" or (args.command is None and interactive)
                or (args.command == "provider" and (args.provider_action is None
                    or (args.provider_action == "add" and args.name is None)))):
            from .tui import run_ui

            run_ui(config)
            return
        if args.command in (None, "serve"):
            from .server import create_server

            create_server(config).run(transport="stdio")
            return
        if args.command == "provider":
            from .providers import add_provider, connect_provider, run_provider

            if args.provider_action == "run":
                forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
                output = run_provider(args.name, forwarded, registry_path=config)
                if output is None:
                    return
            else:
                if args.timeout <= 0:
                    raise ValueError("timeout must be positive")
                options = dict(launcher_dir=args.launcher_dir, executable=args.executable,
                               registry_path=config, timeout=args.timeout)
                if args.provider_action == "add":
                    missing = ["--" + key.replace("_", "-") for key in
                               ("base_url", "env_key", "model", "context_window")
                               if getattr(args, key) is None]
                    if missing:
                        raise ValueError("Missing " + ", ".join(missing)
                                         + "; run codexd-bridge to use the interactive manager")
                    output = add_provider(
                        args.name, args.base_url, args.env_key, args.model, args.context_window,
                        effort=args.effort, wire_api=args.wire_api, home=args.home,
                        model_catalog=args.model_catalog, **options,
                    )
                else:
                    output = connect_provider(args.name, **options)
        elif args.command == "daemon" and args.action == "create":
            from .provisioning import create_daemon

            backend = create_daemon(
                args.name,
                args.home,
                args.provider,
                args.model,
                registry_path=config,
                template_home=args.template_home,
                effort=args.effort,
            )
            output = {
                "backend": backend.name,
                "home": str(backend.home),
                "model_provider": backend.model_provider,
                "started": False,
                "enabled": backend.enabled,
                "model_verified": False,
                "registry": str(config),
            }
        elif args.command == "daemon" and args.action == "list":
            from .sessions import Bridge

            output = Bridge(config).list_backends()
        elif args.command == "models" and args.model_action == "list":
            from .sessions import Bridge

            bridge = Bridge(config)
            if args.backend:
                output = bridge.list_models(args.backend, args.cursor, args.limit)
            else:
                if args.cursor:
                    raise ValueError("Use --backend with --cursor to page through its models")
                output = bridge.list_provider_models(args.limit)
        else:
            backends = load_backends(config)
            if args.name not in backends:
                raise ConfigError("Unknown backend; run daemon list to inspect registered routes")
            backend = backends[args.name]
            if args.command == "models":
                from .probe import check_model

                output = check_model(
                    backend, model=args.model, effort=args.effort, timeout=args.timeout
                )
            elif args.command == "connect":
                from .integration import connect_backend

                executable = args.executable
                if executable is None:
                    candidate = Path(sys.executable).parent / "codexd-bridge"
                    executable = (
                        candidate
                        if candidate.is_file()
                        else Path(shutil.which("codexd-bridge") or "")
                    )
                if (
                    not executable.is_absolute()
                    or not executable.is_file()
                    or not os.access(executable, os.X_OK)
                ):
                    raise ValueError(
                        "Provide an existing executable file at an absolute --executable path"
                    )
                output = connect_backend(backend, executable, config, update_skill=args.update_skill,
                                         allow_session_writes=args.allow_session_writes)
            else:
                from .provisioning import daemon_action

                if args.timeout <= 0:
                    raise ValueError("timeout must be positive")
                output = daemon_action(backend, args.action, timeout=args.timeout)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (BridgeError, ConfigError, ValueError, OSError) as error:
        argument_parser.exit(2, f"codexd-bridge: {error}\n")


if __name__ == "__main__":
    main()
