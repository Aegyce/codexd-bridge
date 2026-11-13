"""Command-line setup and lifecycle operations; session traffic uses MCP."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, ConfigError, load_backends
from .transport import BridgeError


def registry_path(explicit: Path | None) -> Path:
    path = (
        explicit
        if explicit is not None
        else Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    )
    return path.expanduser().resolve()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bridge independent Codex daemons across providers"
    )
    result.add_argument("--config", type=Path, help="Bridge backend registry TOML")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the MCP stdio server (default)")
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
        if args.command in (None, "serve"):
            from .server import create_server

            create_server(config).run(transport="stdio")
            return
        if args.command == "daemon" and args.action == "create":
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
                from .integration import install_skill, register_bridge
                from .probe import check_model
                from .provisioning import set_backend_enabled

                executable = args.executable
                if executable is None:
                    candidate = Path(sys.executable).parent / "codexd-bridge"
                    executable = (
                        candidate
                        if candidate.is_file()
                        else Path(shutil.which("codexd-bridge") or "")
                    )
                if not executable.is_absolute() or not executable.is_file():
                    raise ValueError(
                        "Provide an existing absolute --executable for the MCP service"
                    )
                verification = check_model(backend)
                skill = install_skill(backend.home)
                try:
                    registration = register_bridge(
                        backend.home,
                        executable,
                        config,
                        allow_session_writes=args.allow_session_writes,
                    )
                except Exception as error:
                    raise BridgeError(
                        f"Discovery skill installed at {skill}; MCP registration failed: {error}"
                    ) from error
                if registration.get("effective") is False:
                    raise BridgeError(
                        "MCP registration is overridden by policy; backend was not enabled"
                    )
                set_backend_enabled(backend.name, True, registry_path=config)
                output = {
                    "backend": backend.name,
                    "skill": str(skill),
                    "registration": registration,
                    "verification": verification,
                    "enabled": True,
                }
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
