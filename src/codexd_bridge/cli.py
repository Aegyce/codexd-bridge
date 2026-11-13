"""Command-line setup and lifecycle operations; session traffic uses MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, _resolve_config_path, load_backends
from .transport import BridgeError


def registry_path(explicit: Path | None) -> Path:
    return _resolve_config_path(explicit)[0].absolute()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bridge independent Codex daemons across providers"
    )
    result.add_argument("--config", type=Path, help="Bridge backend registry TOML")
    result.add_argument("--version", action="version", version="codexd-bridge " + __version__)
    commands = result.add_subparsers(dest="command")
    commands.add_parser("ui", help="Open the interactive provider, model and session manager")
    commands.add_parser("serve", help="Run the MCP stdio server")
    commands.add_parser("doctor", help="Check installation and configuration without model requests")
    commands.add_parser("uninstall", help="Uninstall Codex Daemon Bridge; keep independent daemons and their data",
                        description="Remove the Codex Daemon Bridge application, its commands and owned "
                                    "integrations. Independent daemons, provider configuration, credentials "
                                    "and sessions are preserved.")
    install = commands.add_parser("install", help="Connect this Codex home for automatic discovery in new sessions")
    install.add_argument("--home", type=Path, help="Codex home (default: CODEX_HOME or ~/.codex)")
    install.add_argument("--name", default="codex", help="Route name when attaching an unregistered home")
    install.add_argument("--provider", help="Explicit provider when the existing home omits model_provider")
    install.add_argument("--executable", type=Path)
    install.add_argument("--update-skill", action="store_true")
    install.add_argument("--allow-session-writes", action="store_true")
    provider = commands.add_parser("provider", help="Configure providers and codex-NAME launchers")
    provider_actions = provider.add_subparsers(dest="provider_action")
    add = provider_actions.add_parser("add", help="Create and verify a new provider from connection settings")
    add.add_argument("name", nargs="?", help="Omit to open the TUI")
    add.add_argument("--base-url")
    add.add_argument("--env-key", help="API key environment variable NAME, not its value")
    add.add_argument("--model")
    add.add_argument("--context-window", type=int, help="Provider's documented context limit in tokens")
    add.add_argument("--effort")
    add.add_argument("--wire-api", choices=("responses",), default="responses",
                     help="Provider protocol (Responses API required by the supported Codex version)")
    add.add_argument("--model-catalog", type=Path, help="Existing native model catalog; otherwise declare only --model")
    add.add_argument("--home", type=Path, help="New absolute Codex home")
    finish = provider_actions.add_parser("connect", help="Verify an existing route and install its launcher")
    finish.add_argument("name")
    finish.add_argument("--update-skill", action="store_true",
                        help="Replace the installed bridge skill, including local edits, with the bundled version")
    launcher = provider_actions.add_parser("launcher", help="Install a provider command without model verification")
    launcher.add_argument("name")
    for command in (add, finish, launcher):
        command.add_argument("--launcher-dir", type=Path, help="Launcher directory (default: ~/.local/bin)")
        command.add_argument("--executable", type=Path, help="Absolute installed codexd-bridge executable")
    for command in (add, finish):
        command.add_argument("--timeout", type=float, default=60)
    run = provider_actions.add_parser("run", help="Run the selected provider's native Codex CLI")
    # Keep the route and native arguments in one remainder: a separate name
    # positional lets argparse consume a following native `--` delimiter.
    run.add_argument("args", nargs=argparse.REMAINDER, metavar="NAME [ARGS ...]",
                     help="Backend name followed by arguments forwarded unchanged to codex")
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
    remove = actions.add_parser("remove", help="Stop and unregister one daemon; retain its data by default")
    remove.add_argument("name")
    remove.add_argument("--purge", action="store_true", help="Also delete this daemon's configuration, credentials and sessions")
    remove.add_argument("--launcher-dir", type=Path, help="Additional directory for a legacy custom launcher")
    remove.add_argument("--executable", type=Path)
    remove.add_argument("--timeout", type=float, default=45)
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
        if args.command == "uninstall":
            from .installation import uninstall_current

            uninstall_current()
            return
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
        if args.command == "doctor":
            from .diagnostics import diagnose

            output = diagnose(config)
        elif args.command == "install":
            from .integration import install_bridge
            from .providers import _resolve_executable

            home = args.home or Path(os.environ.get("CODEX_HOME") or "~/.codex")
            output = install_bridge(home, _resolve_executable(args.executable), config,
                                    name=args.name, provider=args.provider, update_skill=args.update_skill,
                                    allow_session_writes=args.allow_session_writes)
        elif args.command == "provider":
            from .providers import (
                add_provider,
                connect_provider,
                install_provider_launcher,
                run_provider,
            )

            if args.provider_action == "run":
                if not args.args:
                    raise ValueError("Choose a backend: provider run NAME [Codex arguments ...]")
                output = run_provider(args.args[0], args.args[1:], registry_path=config)
                if output is None:
                    return
            elif args.provider_action == "launcher":
                output = install_provider_launcher(args.name, registry_path=config,
                    launcher_dir=args.launcher_dir, executable=args.executable)
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
                    output = connect_provider(args.name, update_skill=args.update_skill, **options)
        elif args.command == "daemon" and args.action == "remove":
            from .removal import remove_daemon

            output = remove_daemon(args.name, registry_path=config, purge=args.purge,
                                   executable=args.executable,
                                   launcher_dir=args.launcher_dir, timeout=args.timeout)
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
                from .providers import _resolve_executable

                executable = _resolve_executable(args.executable)
                output = connect_backend(backend, executable, config, update_skill=args.update_skill,
                                         allow_session_writes=args.allow_session_writes)
            else:
                from .provisioning import daemon_action

                if args.timeout <= 0:
                    raise ValueError("timeout must be positive")
                output = daemon_action(backend, args.action, timeout=args.timeout)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not output["ok"]:
            raise SystemExit(1)
    except KeyboardInterrupt:
        argument_parser.exit(130, "codexd-bridge: interrupted\n")
    except (BridgeError, ConfigError, ValueError, OSError) as error:
        argument_parser.exit(2, f"codexd-bridge: {error}\n")


if __name__ == "__main__":
    main()
