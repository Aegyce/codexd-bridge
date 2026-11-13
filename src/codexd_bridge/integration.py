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
import sys
import tempfile
from pathlib import Path

from .transport import AppServer, BridgeError

__all__ = ["IntegrationError", "install_skill", "register_bridge"]

SKILL_NAME = "codexd-bridge"
SERVER_NAME = "codexd-bridge"
SERVER_KEY_PATH = "mcp_servers.codexd-bridge"
SESSION_WRITE_TOOLS = ("create_session", "send_message", "fork_session", "steer_session")


class IntegrationError(BridgeError):
    """Installation or registration was refused; nothing was silently overwritten."""


def _default_source() -> Path:
    """Prefer the packaged data-files copy, then the repository checkout.

    Wheels install the canonical skill under ``share/codexd-bridge/skills`` via
    setuptools data-files; editable checkouts keep it at the repository root.
    """
    packaged = Path(sys.prefix) / "share" / "codexd-bridge" / "skills" / SKILL_NAME
    if (packaged / "SKILL.md").is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME


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


def install_skill(home: Path, *, source: Path | None = None) -> Path:
    """Copy the bundled skill into ``home/skills/codexd-bridge`` and return that path.

    ``source`` is a skill directory containing ``SKILL.md``; when omitted, the
    packaged copy under ``sys.prefix`` is preferred and the repository checkout
    is the fallback. Only the bridge's own skill directory is written. An
    identical existing installation is returned unchanged (idempotent); any
    differing content, extra entries, symlinks, or non-directory occupants are
    refused without modification. Other skills are never listed or touched.
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
        raise IntegrationError(f"refusing to overwrite differing skill content: {target}")
    skills_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-install-", dir=skills_dir))
    try:
        shutil.copytree(src, staging, dirs_exist_ok=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def _approve_session_writes(entry: dict) -> dict:
    """Set approval_mode=approve for the four session-mutating tools only.

    Every other field of those tools, all other tool entries, and service-level
    policies such as default_tools_approval_mode are preserved verbatim.
    """
    tools = entry.get("tools")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise IntegrationError("existing tools configuration is not a table; refusing to edit it")
    updated = dict(tools)
    for name in SESSION_WRITE_TOOLS:
        tool = updated.get(name, {})
        if not isinstance(tool, dict):
            raise IntegrationError(f"tool entry {name!r} is not a table; refusing to edit it")
        updated[name] = {**tool, "approval_mode": "approve"}
    return {**entry, "tools": updated}


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
    is written, via ``config/value/write`` at keyPath ``mcp_servers.codexd-bridge``
    with mergeStrategy ``replace``: command/args/enabled/startup_timeout_sec/
    tool_timeout_sec are managed here, every other existing field of that entry is
    preserved, and no other MCP server is touched. Approval policies are never
    changed unless ``allow_session_writes`` is explicitly true, which sets
    ``approval_mode="approve"`` for create_session/send_message/fork_session/
    steer_session only. ``config/mcpServer/reload`` then refreshes MCP
    connections; the daemon itself is not started or restarted. An identical
    entry skips the write. ``effective`` is false with a warning when a
    higher-priority layer overrides the written value. The returned summary
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
        merged = {**existing, **managed}
        if allow_session_writes:
            merged = _approve_session_writes(merged)
        changed = merged != existing
        write_status = None
        version = None
        if changed:
            written = client.call(
                "config/value/write",
                {"keyPath": SERVER_KEY_PATH, "value": merged, "mergeStrategy": "replace"},
            )
            write_status = written.get("status")
            version = written.get("version")
            if write_status not in ("ok", "okOverridden"):
                raise IntegrationError(
                    "config/value/write returned an unexpected status; "
                    "inspect the daemon configuration before retrying"
                )
        client.call("config/mcpServer/reload", None)
    overridden = write_status == "okOverridden"
    return {
        "server": SERVER_NAME,
        "key_path": SERVER_KEY_PATH,
        "changed": changed,
        "write_status": write_status,
        "version": version,
        "effective": not overridden,
        "warning": (
            "a higher-priority configuration layer overrides mcp_servers.codexd-bridge; "
            "the registered value is not in effect"
        )
        if overridden
        else None,
        "reloaded": True,
        "allow_session_writes": bool(allow_session_writes),
    }
