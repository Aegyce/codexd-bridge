"""Remove one registered daemon without uninstalling the bridge application."""

from __future__ import annotations

import errno
import json
import math
import os
import shutil
import socket
import time
from pathlib import Path

from .config import Backend, _resolve_config_path, load_backends
from .installation import (
    detach_home,
    installation_operation,
    load_registrations,
    registration_index,
    script_installation,
)
from .providers import _launcher_text, _resolve_executable, _validate_name
from .provisioning import _atomic_write, _load_registry, _registry_lock, _toml_dumps, daemon_action
from .transport import AppServer, BridgeError, control_socket_path


def _running(home: Path) -> bool:
    """Distinguish an absent listener from an uncertain or inaccessible socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        try:
            with control_socket_path(home) as path:
                connection.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return False
    return True


def _validate_scope(target: Backend, routes: dict[str, Backend], registry: Path,
                    executable: Path, purge: bool):
    home = target.home
    if purge and home.is_symlink():
        raise BridgeError("A symbolic-link daemon home cannot be purged; remove with data retained")
    if home.exists() and not home.is_dir():
        raise BridgeError("The daemon home must be a regular directory")
    resolved = home.resolve()
    for name, other in routes.items():
        if name != target.name:
            other_home = other.home.resolve()
            if other_home == resolved:
                raise BridgeError("Another route shares this home; removing it would affect that route")
            if purge and (
                other_home.is_relative_to(resolved) or resolved.is_relative_to(other_home)
            ):
                raise BridgeError("Another route overlaps this home; remove with data retained")
    if not purge:
        return
    protected = [Path.home(), Path.home() / ".codex", registry, executable, Path.cwd()]
    if os.environ.get("CODEX_HOME"):
        protected.append(Path(os.environ["CODEX_HOME"]).expanduser())
    if (resolved == Path("/") or home.is_mount()
            or any(path.resolve().is_relative_to(resolved) for path in protected)):
        raise BridgeError("Refusing to delete a shared, current, or application directory; remove with data retained")


def _launchers(target: Backend, registry: Path, executable: Path, launcher_dir: Path | None):
    directories = {Path.home() / ".local/bin"}
    if launcher_dir is not None:
        directory = launcher_dir.expanduser()
        if not directory.is_absolute():
            raise ValueError("launcher_dir must be an absolute path")
        directories.add(directory)
    manifest = script_installation(executable)
    if manifest is not None:
        directories.add(Path(manifest["bin_dir"]))
    paths = {directory / ("codex-" + target.name) for directory in directories}
    index = registration_index(executable, registry)
    if index.exists() or index.is_symlink():
        for record in load_registrations(index):
            if Path(record["home"]).resolve() != target.home.resolve():
                continue
            if Path(record["registry"]).resolve() != registry.resolve():
                raise BridgeError("This home is also registered through another registry; remove that association first")
            paths.update(Path(path) for path in record.get("launchers", [])
                         if Path(path).name == "codex-" + target.name)
    return sorted(paths)


def remove_daemon(name: str, *, registry_path: Path | None = None, purge=False,
                  executable: Path | None = None,
                  launcher_dir: Path | None = None, timeout: float = 45,
                  expected_backend: Backend | None = None, progress=None) -> dict:
    """Stop, detach and unregister one backend; delete data only when purge is requested.

    The route remains disabled on failure, so retry cannot silently resume dispatch.
    Delegation history is retained to protect the ancestry of surviving sessions.
    """
    name = _validate_name(name)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    registry = _resolve_config_path(registry_path)[0].absolute()
    executable = _resolve_executable(executable)
    with installation_operation(executable), _registry_lock(registry):
        data = _load_registry(registry)
        routes = load_backends(registry)
        if name not in routes:
            raise BridgeError("Unknown backend; refresh the provider list")
        target = routes[name]
        if expected_backend is not None and target != expected_backend:
            raise BridgeError("The backend changed after selection; refresh and review removal again")
        _validate_scope(target, routes, registry, executable, purge)
        launchers = _launchers(target, registry, executable, launcher_dir)
        identity = target.home.stat() if target.home.exists() else None
        data["backends"][name]["enabled"] = False
        _atomic_write(registry, _toml_dumps(data))
        removed, kept = [], []
        try:
            if progress:
                progress("detach")
            if target.home.exists():
                guide = detach_home(target.home, executable, registry)
                if guide:
                    kept.append(guide)
                if progress:
                    progress("stop")
                if _running(target.home):
                    # Bind the listener to the selected home before asking the native CLI to stop it.
                    with AppServer(target.home):
                        pass
                    daemon_action(target, "stop", timeout=timeout)
                    deadline = time.monotonic() + timeout
                    while _running(target.home):
                        if time.monotonic() >= deadline:
                            raise BridgeError("The daemon still accepts connections after stop")
                        time.sleep(.1)
            if progress:
                progress("remove")
            expected = _launcher_text(executable, registry, name)
            for path in launchers:
                if not os.path.lexists(path):
                    continue
                if path.is_file() and not path.is_symlink():
                    try:
                        owned = path.read_text() == expected
                    except UnicodeError:
                        owned = False
                    if owned:
                        path.unlink()
                        removed.append(str(path))
                        continue
                kept.append(str(path))
            if purge and target.home.exists():
                deadline = time.monotonic() + timeout
                while target.home.exists():
                    _validate_scope(target, routes, registry, executable, True)
                    current = target.home.stat()
                    if identity is None or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                        raise BridgeError("The daemon directory changed during removal")
                    if _running(target.home):
                        raise BridgeError("The daemon started again; its data was retained")
                    try:
                        shutil.rmtree(target.home)
                    except OSError as error:
                        # A stopped daemon's helper can finish writing temporary
                        # files during deletion. Recheck ownership and the listener
                        # before retrying only these transient filesystem races.
                        if error.errno not in (errno.ENOTEMPTY, errno.ENOENT) or time.monotonic() >= deadline:
                            raise
                        time.sleep(.1)
                kept = [path for path in kept if os.path.lexists(path)]
            index = registration_index(executable, registry)
            if index.exists():
                with _registry_lock(index):
                    records = load_registrations(index)
                    records = [record for record in records if not (
                        Path(record["home"]).resolve() == target.home.resolve()
                        and Path(record["registry"]).resolve() == registry.resolve())]
                    _atomic_write(index, json.dumps(records, indent=2) + "\n")
            del data["backends"][name]
            _atomic_write(registry, _toml_dumps(data))
        except (BridgeError, OSError, ValueError) as error:
            raise BridgeError(f"Removal of {name} is incomplete; its route remains disabled. "
                              f"Inspect it before retrying. {error}") from error
    return {"backend": name, "home": str(target.home), "removed": True,
            "data_deleted": bool(purge), "removed_launchers": removed, "kept_files": kept}
