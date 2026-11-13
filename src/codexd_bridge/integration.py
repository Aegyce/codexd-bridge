"""Install the bundled skill and register the bridge with a running daemon.

The bridge skill and its canonical MCP entry are managed inside a Codex home.
Recognized compatibility entries receive the same caller identity and version.
Other skills, MCP servers, and approval policies are never
modified; session-write approvals change only when the caller explicitly opts
in. Configuration is read through the daemon's own ``config/read`` method, only
the owned service block is inspected, and no configuration content or credential
is ever echoed. Registration does not restart daemons; the setup workflow starts
a stopped daemon when needed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

from . import __version__
from .transport import AppServer, BridgeError

__all__ = ["IntegrationError", "install_bridge", "install_skill", "register_bridge"]

SKILL_NAME = "codexd-bridge"
SERVER_NAME = "codexd-bridge"
SERVER_KEY_PATH = "mcp_servers.codexd-bridge"
SESSION_WRITE_TOOLS = ("create_session", "send_message", "fork_session", "steer_session")
BRIDGE_TOOLS = ("list_backends", "list_models", "list_provider_models", "list_sessions",
                "read_session", "read_messages", *SESSION_WRITE_TOOLS)


def configure_home_provider(home: Path, provider: str) -> None:
    """Persist a provider explicitly selected during setup, preserving existing settings."""
    from .provisioning import _atomic_write, _registry_lock, _text

    provider = _text(provider, "model_provider")
    config = home / "config.toml"
    if not home.is_absolute() or not home.is_dir():
        raise IntegrationError("Choose an existing absolute Codex home")
    with _registry_lock(config):
        if config.is_symlink() or (config.exists() and not config.is_file()):
            raise IntegrationError("Codex configuration must be a regular file")
        try:
            original = config.read_text() if config.exists() else ""
            data = tomllib.loads(original)
        except (OSError, ValueError):
            raise IntegrationError("Cannot read the Codex configuration") from None
        existing = data.get("model_provider")
        if existing is not None:
            if existing != provider:
                raise IntegrationError("Selected provider differs from the existing configuration; "
                                       "choose its configured provider or use a separate home")
            return
        # Prefix a top-level setting, preserving comments, tables and unrelated content.
        _atomic_write(config, "model_provider = " + json.dumps(provider) + "\n" + original)


def install_bridge(home: Path, executable: Path, registry: Path, *, name="codex",
                   provider: str | None = None, update_skill=False, allow_session_writes=False,
                   progress=None):
    """Attach the selected Codex home and persist discovery for future sessions."""
    from .config import load_backends
    from .providers import _ensure_running, _validate_name
    from .provisioning import attach_daemon

    home = home.expanduser()
    if not home.is_absolute():
        raise IntegrationError("home must be an absolute path")
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise IntegrationError("Provide an existing executable file at an absolute --executable path")
    if progress:
        progress("configuration")
    routes = load_backends(registry) if registry.exists() else {}
    matching = [b for b in routes.values() if b.home.resolve() == home.resolve()]
    if len(matching) > 1:
        raise IntegrationError("Home has multiple routes; use connect with an explicit backend")
    if not matching and name in routes:
        raise IntegrationError("This provider alias is already in use; choose another alias")
    if not matching:
        _validate_name(name)
    if registry.resolve() == home.resolve() or home.resolve() in registry.resolve().parents:
        raise IntegrationError("Choose a bridge registry outside the Codex directory")
    if provider is not None:
        if matching and matching[0].model_provider != provider:
            raise IntegrationError("Selected provider differs from the existing bridge route")
        configure_home_provider(home, provider)
    backend = matching[0] if matching else attach_daemon(name, home, registry_path=registry)
    if progress:
        progress("daemon")
    _ensure_running(backend, 45)
    return connect_backend(backend, executable, registry, update_skill=update_skill,
                           allow_session_writes=allow_session_writes, progress=progress)


def _field_edits(existing: dict, managed: dict, prefix: str = SERVER_KEY_PATH) -> list[dict]:
    """Update only owned leaves, preserving unrelated and concurrently edited fields."""
    edits = []
    for key, value in managed.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            current = existing.get(key, {})
            if not isinstance(current, dict):
                raise IntegrationError(f"configuration {path} is not a table")
            edits.extend(_field_edits(current, value, path))
        elif existing.get(key) != value:
            edits.append({"keyPath": path, "value": value, "mergeStrategy": "replace"})
    return edits


def _compatibility_entries(servers: dict, executable: Path, registry: Path | None):
    """Recognize only legacy entries using this installation and registry."""
    for name in ("codex-bridge", "codex-qwen"):
        entry = servers.get(name, {})
        if not isinstance(entry, dict) or "url" in entry:
            continue
        args = entry.get("args", [])
        canonical = (entry.get("command") == str(executable)
                     and (registry is None or args in (["--config", str(registry)],
                                  ["--config", str(registry), "serve"])))
        compat = (entry.get("command") == str(executable.parent / "python")
                  and isinstance(args, list) and len(args) == 8
                  and args[:3] == ["-m", "codexd_bridge.compat", "--config"]
                  and (registry is None or args[3] == str(registry))
                  and args[4] == "--backend" and args[6] == "--cwd")
        if canonical or compat:
            yield name, entry


class IntegrationError(BridgeError):
    """Installation or registration was refused; nothing was silently overwritten."""


def user_config(read: dict, home: Path) -> dict:
    """Get the durable base user layer, excluding profiles and process-only flags."""
    layers = read.get("layers")
    if not isinstance(layers, list):
        raise IntegrationError("Daemon did not return configuration layers; persistence cannot be verified")
    for layer in layers:
        source = layer.get("name", {})
        if (source.get("type") == "user" and source.get("profile") is None
                and Path(source.get("file", "")).resolve() == (home / "config.toml").resolve()):
            config = layer.get("config")
            if not isinstance(config, dict):
                raise IntegrationError("User configuration layer is not a table")
            return config
    return {}


def connect_backend(backend, executable: Path, registry: Path, *, update_skill=False,
                    allow_session_writes=False, progress=None):
    """Verify and connect a running backend, shared by the CLI and terminal UI."""
    from .probe import check_model
    from .provisioning import set_backend_enabled

    if progress:
        progress("verification")
    verification = check_model(backend)
    if progress:
        progress("integration")
    skill = install_skill(backend.home, update=update_skill)
    try:
        registration = register_bridge(backend.home, executable, registry,
                                       allow_session_writes=allow_session_writes)
    except Exception as error:
        raise BridgeError(f"Discovery skill installed at {skill}; MCP registration failed: {error}") from error
    if registration.get("effective") is False:
        raise BridgeError("MCP registration is not effective; enabling was skipped. "
                          + str(registration.get("warning") or "Check tool discovery and configuration policy"))
    try:
        set_backend_enabled(backend.name, True, registry_path=registry, expected_backend=backend)
    except (ValueError, OSError) as error:
        raise BridgeError(f"Model verified; skill installed at {skill}; MCP registered. "
                          f"Enabling backend {backend.name} failed: {error}") from error
    if progress:
        progress("ready")
    return {"backend": backend.name, "skill": str(skill), "registration": registration,
            "verification": verification, "enabled": True}


def _default_source() -> Path:
    """Keep resources beside the package for venv, user and editable installs."""
    return Path(__file__).resolve().with_name("skill")


def _manifest(root: Path) -> dict:
    """Map every relative entry to its kind and content, without following links."""
    entries: dict = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = ("symlink", str(path.readlink()))
        elif path.is_dir():
            entries[rel] = ("dir",)
        elif path.is_file():
            entries[rel] = ("file", path.read_bytes())
        else:
            entries[rel] = ("special",)
    return entries


def install_skill(home: Path, *, source: Path | None = None, update: bool = False) -> Path:
    """Copy the bundled skill into ``home/skills/codexd-bridge`` and return that path.

    ``source`` is a skill directory containing ``SKILL.md``; when omitted, the
    package's own resource directory is used. Only the bridge's skill directory
    is written. An identical installation is returned unchanged; any
    differing installation is preserved unless ``update`` is explicitly true.
    Updates stage the full new tree and restore the old tree if replacement
    fails. Symlinks and non-directory occupants are always refused.
    """
    home = Path(home).expanduser()
    if not home.is_absolute():
        raise IntegrationError("home must be an absolute path")
    src = Path(source).expanduser().resolve() if source is not None else _default_source()
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        raise IntegrationError(f"skill source must be a directory containing SKILL.md: {src}")
    if any(path.is_symlink() for path in src.rglob("*")):
        raise IntegrationError("skill source must not contain symlinks")
    skills_dir = home / "skills"
    target = skills_dir / SKILL_NAME
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise IntegrationError(f"refusing to replace a non-directory skill entry: {target}")
    if target.exists():
        if _manifest(src) == _manifest(target):
            return target
        if not update:
            raise IntegrationError(
                f"refusing to overwrite differing skill content: {target}; "
                "review local changes, then use connect --update-skill to replace it"
            )
        if any(p.is_symlink() for p in target.rglob("*")):
            raise IntegrationError("refusing to update a skill containing symlinks")
    skills_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-install-", dir=skills_dir))
    backup = None
    try:
        shutil.copytree(src, staging, dirs_exist_ok=True)
        if target.exists():
            if not update:
                raise IntegrationError(
                    "skill appeared during installation; inspect it before retrying"
                )
            backup = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-previous-", dir=skills_dir))
            target.rename(backup / "skill")
        try:
            staging.rename(target)
        except BaseException:
            if backup is not None:
                (backup / "skill").rename(target)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if backup is not None and not (backup / "skill").exists():
            backup.rmdir()
    if backup is not None and backup.exists():
        shutil.rmtree(backup)
    return target


def register_bridge(
    home: Path,
    executable: Path,
    registry_path: Path,
    *,
    client_factory=AppServer,
    allow_session_writes: bool = False,
    refresh_only: bool = False,
) -> dict:
    """Register ``mcp_servers.codexd-bridge`` on an already running daemon.

    The daemon at ``home`` must already be running; it is contacted through
    ``client_factory`` (``AppServer`` by default). Only the owned service entry
    is updated, via ``config/batchWrite`` of its managed leaf fields:
    command/args/enabled/timeouts and its version marker in env. Other fields
    are preserved, including concurrent changes by another client. Approval
    policies change only when ``allow_session_writes`` is explicitly true, which sets
    ``approval_mode="approve"`` for create_session/send_message/fork_session/
    steer_session only. ``config/mcpServer/reload`` then refreshes MCP
    connections; the daemon itself is not started or restarted. An identical
    effective and persisted entry skips the write. ``skills/list`` refreshes discovery from disk.
    ``effective`` is false when a policy overrides the update, the new MCP
    worker lacks tools, or the bundled skill is not enabled. The returned summary
    contains no configuration values or credentials.
    """
    home = Path(home).expanduser()
    executable = Path(executable).expanduser()
    registry_path = Path(registry_path).expanduser()
    for label, path in (
        ("home", home),
        ("executable", executable),
        ("registry_path", registry_path),
    ):
        if not path.is_absolute():
            raise IntegrationError(f"{label} must be an absolute path")
    from .installation import installation_operation, owned_servers, remember_registration

    with installation_operation(executable):
        managed = {
            "command": str(executable),
            "args": ["--config", str(registry_path), "serve"],
            "enabled": True,
            "startup_timeout_sec": 20,
            "tool_timeout_sec": 60,
            # A changed server identity lets the daemon replace old MCP workers on upgrade.
            "env": {
                "CODEXD_BRIDGE_VERSION": __version__,
                "CODEXD_BRIDGE_CALLER_HOME": str(home.resolve()),
            },
        }
        with client_factory(home) as client:
            read = client.call("config/read", {"includeLayers": True})
            config = read.get("config")
            if not isinstance(config, dict):
                raise IntegrationError("config/read returned an unexpected shape; no write attempted")
            servers = config.get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise IntegrationError("mcp_servers configuration is not a table; no write attempted")
            existing = servers.get(SERVER_NAME, {})
            if not isinstance(existing, dict):
                raise IntegrationError("existing codexd-bridge entry is not a table; refusing it")
            if "url" in existing:
                raise IntegrationError(
                    "existing codexd-bridge entry is URL-based; refusing to convert its transport"
                )
            persisted_servers = user_config(read, home).get("mcp_servers", {})
            if not isinstance(persisted_servers, dict):
                raise IntegrationError("Persisted mcp_servers configuration is not a table")
            persisted = persisted_servers.get(SERVER_NAME, {})
            if not isinstance(persisted, dict):
                raise IntegrationError("Persisted codexd-bridge entry is not a table")
            if "url" in persisted:
                raise IntegrationError("Persisted codexd-bridge entry is URL-based; refusing to convert its transport")
            if refresh_only and (SERVER_NAME not in owned_servers(
                    {"mcp_servers": persisted_servers}, executable, registry_path)
                    or persisted.get("enabled") is False):
                return {"skipped": True, "new_sessions_ready": False}
            remember_registration(home, executable, registry_path)
            if allow_session_writes:
                managed["tools"] = {name: {"approval_mode": "approve"} for name in SESSION_WRITE_TOOLS}
            edits = _field_edits(existing, managed)
            edited = {edit["keyPath"] for edit in edits}
            edits.extend(edit for edit in _field_edits(persisted, managed) if edit["keyPath"] not in edited)
            aliases = []
            for name, entry in _compatibility_entries(persisted_servers, executable, registry_path):
                edits.extend(_field_edits(entry, {"env": managed["env"]}, f"mcp_servers.{name}"))
                aliases.append(name)
            changed = bool(edits)
            write_status = None
            version = None
            if changed:
                written = client.call(
                    "config/batchWrite",
                    {"edits": edits, "filePath": str(home / "config.toml"), "reloadUserConfig": True},
                )
                write_status = written.get("status")
                version = written.get("version")
                if write_status not in ("ok", "okOverridden"):
                    raise IntegrationError(
                        "config/batchWrite returned an unexpected status; "
                        "inspect the daemon configuration before retrying"
                    )
            client.call("config/mcpServer/reload", None)
            inventory = client.call("mcpServerStatus/list", {"serverName": SERVER_NAME, "limit": 1})
            ready = any(
                entry.get("name") == SERVER_NAME
                and not entry.get("toolsError")
                and set(BRIDGE_TOOLS).issubset({
                    tool.get("name", key) for key, tool in entry.get("tools", {}).items()
                })
                and (entry.get("serverInfo") or {}).get("version") == __version__
                for entry in inventory.get("data", [])
            )
            skills = client.call("skills/list", {"cwds": [str(home)], "forceReload": True})
            expected_skill = (home / "skills" / SKILL_NAME / "SKILL.md").resolve()
            skill_ready = any(
                skill.get("name") == SKILL_NAME and skill.get("enabled") is True
                and Path(skill.get("path", "")).resolve() == expected_skill
                for entry in skills.get("data", []) for skill in entry.get("skills", [])
            )
        overridden = write_status == "okOverridden"
        return {
            "server": SERVER_NAME,
            "key_path": SERVER_KEY_PATH,
            "changed": changed,
            "write_status": write_status,
            "version": version,
            "effective": not overridden and ready and skill_ready,
            "tools_ready": ready,
            "skill_ready": skill_ready,
            "new_sessions_ready": not overridden and ready and skill_ready,
            "warning": (
                "a higher-priority configuration layer overrides mcp_servers.codexd-bridge; "
                "the registered value is not in effect"
            )
            if overridden
            else ("MCP tool discovery did not confirm the complete installed bridge version"
                  if not ready else (None if skill_ready else
                                     "Codex did not discover the enabled bridge skill; inspect skills/list")),
            "reloaded": True,
            "compatibility_entries": aliases,
            "session_write_approval_requested": bool(allow_session_writes),
        }
