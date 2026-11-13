"""Install the bundled skill and register the bridge with a running daemon.

Only two artifacts are managed: the ``skills/codexd-bridge`` directory inside a
Codex home, and the ``mcp_servers.codexd-bridge`` entry of a running daemon's
configuration. Other skills, MCP servers, and approval policies are never
modified; session-write approvals change only when the caller explicitly opts
in. Configuration is read through the daemon's own ``config/read`` method, only
the owned service block is inspected, and no configuration content or credential
is ever echoed. No daemon is started or restarted.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import __version__
from .transport import AppServer, BridgeError

__all__ = ["IntegrationError", "install_skill", "register_bridge"]

SKILL_NAME = "codexd-bridge"
SERVER_NAME = "codexd-bridge"
SERVER_KEY_PATH = "mcp_servers.codexd-bridge"
SESSION_WRITE_TOOLS = ("create_session", "send_message", "fork_session", "steer_session")


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


class IntegrationError(BridgeError):
    """Installation or registration was refused; nothing was silently overwritten."""


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
    entry skips the write. ``effective`` is false when a policy overrides the
    update or the new MCP worker cannot expose its tools. The returned summary
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
    managed = {
        "command": str(executable),
        "args": ["--config", str(registry_path), "serve"],
        "enabled": True,
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 60,
        # A changed server identity lets the daemon replace old MCP workers on upgrade.
        "env": {"CODEXD_BRIDGE_VERSION": __version__},
    }
    with client_factory(home) as client:
        read = client.call("config/read", {"includeLayers": False})
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
        if allow_session_writes:
            managed["tools"] = {name: {"approval_mode": "approve"} for name in SESSION_WRITE_TOOLS}
        edits = _field_edits(existing, managed)
        changed = bool(edits)
        write_status = None
        version = None
        if changed:
            written = client.call(
                "config/batchWrite",
                {"edits": edits, "reloadUserConfig": True},
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
            and bool(entry.get("tools"))
            and (entry.get("serverInfo") or {}).get("version") == __version__
            for entry in inventory.get("data", [])
        )
    overridden = write_status == "okOverridden"
    return {
        "server": SERVER_NAME,
        "key_path": SERVER_KEY_PATH,
        "changed": changed,
        "write_status": write_status,
        "version": version,
        "effective": not overridden and ready,
        "tools_ready": ready,
        "warning": (
            "a higher-priority configuration layer overrides mcp_servers.codexd-bridge; "
            "the registered value is not in effect"
        )
        if overridden
        else (None if ready else "MCP tool discovery did not confirm the installed bridge version"),
        "reloaded": True,
        "session_write_approval_requested": bool(allow_session_writes),
    }
