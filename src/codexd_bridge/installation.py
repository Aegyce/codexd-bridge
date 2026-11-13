"""Track and disable integrations owned by the user-local script installation."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shlex
import shutil
import tomllib
from contextlib import contextmanager
from pathlib import Path

from .provisioning import _atomic_write, _registry_lock
from .transport import AppServer, BridgeError


def load_registrations(index: Path) -> list[dict]:
    """Read bounded ownership metadata without following a replaced index file."""
    if not index.exists() and not index.is_symlink():
        return []
    try:
        descriptor = os.open(index, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            import stat

            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("non-regular file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("size limit")
        records = json.loads(raw)
        if not isinstance(records, list):
            raise ValueError("expected a list")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("launchers", []), list):
                raise ValueError("invalid record")
            for value in [record.get("home"), record.get("registry"), *record.get("launchers", [])]:
                if (not isinstance(value, str) or not Path(value).is_absolute()
                        or any(ord(char) < 32 for char in value)):
                    raise ValueError("invalid path")
        return records
    except (OSError, ValueError, TypeError) as error:
        raise BridgeError(f"Cannot read installation ownership records at {index}; repair or restore this file") from error


def uninstall_current():
    """Run the installed standalone uninstaller before deleting this Python environment."""
    from .providers import _resolve_executable

    executable = _resolve_executable(None)
    root = executable.parent.parent
    marker = root / "installation.json"
    helper = root / "manage_install.py"
    if not marker.is_file() or marker.is_symlink() or not helper.is_file() or helper.is_symlink():
        raise BridgeError("This copy was not installed by install.sh; use its original package manager to uninstall it")
    script_installation(executable)
    python = root / "current/bin/python"
    os.execv(str(python), [str(python), "-I", str(helper), "uninstall", "--prefix", str(root)])


def script_installation(executable: Path) -> dict | None:
    """Recognize a script installation and reject corrupt ownership before mutations."""
    root = executable.parent.parent
    marker = root / "installation.json"
    if executable != root / "bin/codexd-bridge" or not os.path.lexists(marker):
        return None
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024 * 1024:
            raise ValueError
        manifest = json.loads(marker.read_bytes())
        if (not isinstance(manifest, dict) or manifest.get("application") != "codexd-bridge"
                or type(manifest.get("version")) is not int or manifest["version"] != 1
                or manifest.get("root") != str(root)
                or not isinstance(manifest.get("bin_dir"), str)
                or not Path(manifest["bin_dir"]).is_absolute()):
            raise ValueError
        return manifest
    except (OSError, ValueError, TypeError):
        raise BridgeError("Installation marker is invalid; restore it before changing integrations") from None


def registration_index(executable: Path, registry: Path) -> Path:
    if script_installation(executable) is not None:
        return executable.parent.parent / "registrations.json"
    return registry.with_name(registry.name + ".integrations.json")


@contextmanager
def installation_operation(executable: Path):
    """Keep script uninstall/upgrade from racing a native registration or launcher write."""
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BridgeError("Bridge executable is unavailable; reinstall before connecting")
    if script_installation(executable) is None:
        yield
        return
    root = executable.parent.parent
    fd = os.open(root.parent / ("." + root.name + ".install.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("Bridge installation is being changed; reconnect after installation finishes") from None
        if script_installation(executable) is None or not executable.is_file():
            raise BridgeError("Bridge was uninstalled; reinstall before connecting")
        yield
    finally:
        os.close(fd)


def remember_registration(home: Path, executable: Path, registry: Path, *, launcher: Path | None = None):
    index = registration_index(executable, registry)
    with _registry_lock(index):
        records = load_registrations(index)
        record = next((record for record in records if record.get("home") == str(home)
                       and record.get("registry") == str(registry)), None)
        changed = False
        if record is None:
            record = {"home": str(home), "registry": str(registry)}
            records.append(record)
            changed = True
        if launcher is not None and str(launcher) not in record.setdefault("launchers", []):
            record["launchers"].append(str(launcher))
            changed = True
        if changed:
            _atomic_write(index, json.dumps(records, indent=2) + "\n")


def owned_servers(config: dict, executable: Path, registry: Path | None = None) -> list[str]:
    from .integration import SERVER_NAME, _compatibility_entries

    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise BridgeError("MCP configuration must be a table; no integrations were removed")
    names = [name for name, _entry in _compatibility_entries(servers, executable, registry)]
    entry = servers.get(SERVER_NAME, {})
    if (isinstance(entry, dict) and "url" not in entry
            and entry.get("command") == str(executable)
            and (registry is None or entry.get("args") in (
                ["--config", str(registry)], ["--config", str(registry), "serve"]))):
        names.insert(0, SERVER_NAME)
    return names


def disabled_config(original: str, executable: Path, registry: Path | None = None) -> str:
    """Edit standard native MCP tables and verify all other TOML values are unchanged."""
    data = tomllib.loads(original)
    expected = copy.deepcopy(data)
    lines = original.splitlines(keepends=True)
    for name in owned_servers(data, executable, registry):
        if data["mcp_servers"][name].get("enabled") is False:
            continue
        expected["mcp_servers"][name]["enabled"] = False
        found = False
        for start, line in enumerate(lines):
            try:
                header = tomllib.loads(line.strip() + "\n__bridge_table__ = true\n")
            except tomllib.TOMLDecodeError:
                continue
            table = header.get("mcp_servers", {}).get(name, {})
            if not isinstance(table, dict) or table.get("__bridge_table__") is not True:
                continue
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].lstrip().startswith("[")), len(lines))
            enabled = next((i for i in range(start + 1, end)
                            if re.match(r'\s*(enabled|"enabled"|\'enabled\')\s*=', lines[i])), None)
            if enabled is None:
                lines.insert(start + 1, "enabled = false\n")
            else:
                lines[enabled] = "enabled = false\n"
            found = True
            break
        if not found:
            raise BridgeError("Cannot safely disable this MCP configuration while Codex is offline; "
                              "open Codex and run the uninstaller again")
    updated = "".join(lines)
    if tomllib.loads(updated) != expected:
        raise BridgeError("MCP configuration edit could not be verified; installation was retained")
    return updated


def detach_installation(root: Path):
    index = root / "registrations.json"
    if not os.path.lexists(index):
        return
    executable = root / "bin/codexd-bridge"
    manifest = script_installation(executable)
    if manifest is None:
        raise BridgeError("Script installation marker is missing; nothing was removed")
    records = load_registrations(index)
    for record in records:
        for field in ("home", "registry"):
            if Path(record[field]).resolve().is_relative_to(root.resolve()):
                raise BridgeError("Backend data is inside the application directory; move the backend "
                                  "home or registry outside it before uninstalling. Nothing was removed")
    for home in sorted({Path(record["home"]) for record in records}):
        kept = detach_home(home, executable)
        if kept:
            print(f"Kept a shared or modified bridge guide: {kept}")
    directory = Path(manifest["bin_dir"])
    launchers = set(directory.glob("codex-*"))
    launchers.update(Path(path) for record in records for path in record.get("launchers", []))
    for launcher in sorted(launchers):
        if launcher.is_file() and not launcher.is_symlink():
            try:
                text = launcher.read_text()
                words = shlex.split(text, comments=True)
            except (OSError, UnicodeError, ValueError):
                continue
            from .providers import _launcher_text

            if (len(words) == 8 and words[:3] == ["exec", str(executable), "--config"]
                    and words[4:6] == ["provider", "run"] and words[7] == "$@"
                    and text == _launcher_text(executable, Path(words[3]), words[6])):
                launcher.unlink()


def refresh_installation(root: Path, previous: Path):
    """Refresh owned, enabled integrations after publishing a script upgrade."""
    from .integration import SERVER_NAME, _manifest, install_skill, register_bridge

    executable = root / "bin/codexd-bridge"
    try:
        if script_installation(executable) is None:
            raise BridgeError("Installation marker is missing")
        records = load_registrations(root / "registrations.json")
    except BridgeError:
        print("Application updated; integration ownership could not be verified. Repair the ownership records and reconnect.")
        return
    old_guides = list(previous.glob("lib/python*/site-packages/codexd_bridge/skill"))
    for record in records:
        home, registry = Path(record["home"]), Path(record["registry"])
        config_path = home / "config.toml"
        try:
            if not config_path.exists():
                continue
            if config_path.is_symlink() or not config_path.is_file():
                raise BridgeError("Codex configuration is not a regular file")
            config = tomllib.loads(config_path.read_text())
            names = owned_servers(config, executable, registry)
            if SERVER_NAME not in names:
                if any(config["mcp_servers"][name].get("enabled") is not False for name in names):
                    print(f"Application updated; reconnect compatibility-only integration at {home} through Connect bridge.")
                continue
            if config["mcp_servers"][SERVER_NAME].get("enabled") is False:
                continue
            guide = home / "skills/codexd-bridge"
            with installation_operation(executable):
                unchanged = (len(old_guides) == 1 and guide.is_dir() and not guide.is_symlink()
                             and _manifest(guide) == _manifest(old_guides[0]))
                try:
                    install_skill(home, update=unchanged)
                except BridgeError:
                    print(f"Kept a differing bridge guide at {home}; review it with Connect bridge.")
            result = register_bridge(home, executable, registry, refresh_only=True)
            if result.get("skipped"):
                print(f"Kept a changed or disabled integration at {home}; refresh skipped.")
                continue
            if not result["new_sessions_ready"]:
                raise BridgeError("MCP discovery did not confirm the updated integration")
            print(f"Updated bridge integration: {home}")
        except BridgeError as error:
            if isinstance(error.__cause__, (ConnectionRefusedError, FileNotFoundError)):
                print(f"Updated files for stopped daemon: {home}; it will use the new program when started.")
            else:
                print(f"Application updated; reconnect {home} through Connect bridge to finish integration refresh.")
        except (OSError, ValueError):
            print(f"Application updated; inspect the configuration at {home} and reconnect through Connect bridge.")


def detach_home(home: Path, executable: Path, registry: Path | None = None):
    """Disable only this bridge integration; preserve the home and other integrations."""
    from .integration import SERVER_NAME, _default_source, _manifest, user_config

    config_path = home / "config.toml"
    if not config_path.exists():
        return
    if config_path.is_symlink() or not config_path.is_file():
        raise BridgeError("Cannot edit a non-regular Codex configuration; installation was retained")
    try:
        connection = AppServer(home)
    except BridgeError as error:
        # Only a definitely absent listener permits an offline edit. Handshake,
        # identity and timeout errors must retain the installation for retry.
        if not isinstance(error.__cause__, (ConnectionRefusedError, FileNotFoundError)):
            raise
        connection = None
    if connection is not None:
        with connection as client:
            read = client.call("config/read", {"includeLayers": True})
            persisted = user_config(read, home)
            names = sorted(set(owned_servers(read["config"], executable, registry))
                           | set(owned_servers(persisted, executable, registry)))
            servers = {**persisted.get("mcp_servers", {}), **read["config"].get("mcp_servers", {})}
            edits = [{"keyPath": "mcp_servers." + name + ".enabled", "value": False,
                      "mergeStrategy": "replace"} for name in names]
            if edits:
                result = client.call("config/batchWrite", {
                    "edits": edits, "filePath": str(config_path), "reloadUserConfig": True})
                if result.get("status") != "ok":
                    raise BridgeError("MCP disable was overridden; installation was retained")
                client.call("config/mcpServer/reload", None)
    else:
        with _registry_lock(config_path):
            original = config_path.read_text()
            config = tomllib.loads(original)
            names = owned_servers(config, executable, registry)
            servers = config.get("mcp_servers", {})
            updated = disabled_config(original, executable, registry)
            if updated != original:
                _atomic_write(config_path, updated)
    skill = home / "skills/codexd-bridge"
    if names and skill.is_dir() and not skill.is_symlink():
        if any(name in servers and name not in names for name in (SERVER_NAME, "codex-bridge", "codex-qwen")):
            return str(skill)
        if _manifest(skill) == _manifest(_default_source()):
            shutil.rmtree(skill)
        else:
            return str(skill)
