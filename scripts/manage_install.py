"""Install or uninstall Codex Daemon Bridge itself; preserve independent daemons and their data."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 -- Runs before installing the Python package.
    raise SystemExit("Python 3.11 or newer is required.")

APP = "codexd-bridge"
MARKER = "installation.json"
PATH_BEGIN = "# >>> codexd-bridge PATH >>>"
PATH_END = "# <<< codexd-bridge PATH <<<"


@dataclass(frozen=True)
class Layout:
    home: Path
    root: Path
    bin_dir: Path

    @property
    def command(self):
        return self.root / "bin" / APP


def read_text(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def atomic_text(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codexd-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_link(path: Path, target: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codexd-install-", dir=path.parent)
    os.close(fd)
    Path(temporary).unlink()
    try:
        Path(temporary).symlink_to(target)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(root: Path):
    root.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(root.parent / ("." + root.name + ".install.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def manifest(root: Path) -> dict | None:
    if root.is_symlink():
        raise ValueError("Refusing a symbolic link as the application directory")
    if not root.exists():
        return None
    marker = root / MARKER
    if not root.is_dir() or not marker.is_file() or marker.is_symlink():
        raise ValueError(f"{root} is not an installation managed by this script")
    data = json.loads(marker.read_text())
    if (not isinstance(data, dict) or data.get("application") != APP
            or type(data.get("version")) is not int or data["version"] != 1
            or data.get("root") != str(root)):
        raise ValueError("Installation marker is invalid; no files were removed")
    directory, profiles = data.get("bin_dir"), data.get("profiles", [])
    if (not isinstance(directory, str) or not Path(directory).is_absolute()
            or Path(directory).resolve().is_relative_to(root.resolve())
            or not isinstance(profiles, list)):
        raise ValueError("Installation paths are invalid; no files were removed")
    for profile in profiles:
        if (not isinstance(profile, dict) or not isinstance(profile.get("path"), str)
                or not Path(profile["path"]).is_absolute()
                or not isinstance(profile.get("block"), str)
                or not profile["block"].lstrip("\n").startswith(PATH_BEGIN + "\n")
                or not profile["block"].endswith(PATH_END + "\n")
                or type(profile.get("created")) is not bool):
            raise ValueError("Installation shell-profile records are invalid; no files were removed")
    return data


def require_owned_link(link: Path, target: Path):
    if os.path.lexists(link) and not (link.is_symlink() and link.readlink() == target):
        raise ValueError(f"Another file already uses {link}; it was left unchanged")


def path_block(bin_dir: Path, *, fish=False) -> str:
    quoted = shlex.quote(str(bin_dir))
    body = (f"fish_add_path --path {quoted}" if fish else
            f'case ":$PATH:" in\n    *:{quoted}:*) ;;\n    *) export PATH={quoted}:"$PATH" ;;\nesac')
    return f"{PATH_BEGIN}\n{body}\n{PATH_END}\n"


def profile_paths(home: Path, shell: str) -> list[tuple[Path, bool]]:
    if Path(shell).name == "fish":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").expanduser()
        if not config.is_absolute():
            raise ValueError("XDG_CONFIG_HOME must be an absolute directory")
        return [(config / "fish/conf.d/codexd-bridge.fish", True)]
    if Path(shell).name == "zsh":
        config = Path(os.environ.get("ZDOTDIR") or home).expanduser()
        if not config.is_absolute():
            raise ValueError("ZDOTDIR must be an absolute directory")
        return [(config / ".zshrc", False), (config / ".zprofile", False)]
    login = next((home / name for name in (".bash_profile", ".bash_login")
                  if (home / name).exists()), home / ".profile")
    return [(home / ".bashrc", False), (login, False)]


def profile_snapshots(layout: Layout, shell: str):
    profiles = []
    for path, fish in profile_paths(layout.home, shell):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Cannot add the command path to a non-regular profile: {path}")
        content = read_text(path) if path.exists() else ""
        block = path_block(layout.bin_dir, fish=fish)
        if PATH_BEGIN in content and block not in content:
            raise ValueError(f"The bridge PATH block was edited in {path}; preserve or remove it first")
        profiles.append((path, content, block, path.exists()))
    return profiles


def build_release(source: Path, release: Path):
    subprocess.run([sys.executable, "-I", "-m", "venv", str(release)], check=True)
    python = release / "bin/python"
    subprocess.run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                    "--require-hashes", "-r", str(source / "requirements.lock")], check=True)
    subprocess.run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-deps", str(source)], check=True)
    subprocess.run([str(python), "-I", "-c",
                    "from codexd_bridge.server import create_server; create_server()"], check=True)


class FileChanges:
    """Roll back launcher/config changes if publishing a built release fails."""

    def __init__(self):
        self.before = {}

    def save(self, path: Path):
        if path in self.before:
            return
        if path.is_symlink():
            self.before[path] = ("link", path.readlink())
        elif path.exists():
            self.before[path] = ("file", read_text(path), path.stat().st_mode & 0o777)
        else:
            self.before[path] = ("absent",)

    def text(self, path, text, mode=0o600):
        self.save(path)
        atomic_text(path, text, mode)

    def link(self, path, target):
        self.save(path)
        atomic_link(path, target)

    def rollback(self):
        for path, record in reversed(list(self.before.items())):
            if record[0] == "absent":
                path.unlink(missing_ok=True)
            elif record[0] == "link":
                atomic_link(path, record[1])
            else:
                atomic_text(path, record[1], record[2])


def install(source: Path, layout: Layout, *, shell: str):
    if not (source / "pyproject.toml").is_file() or not (source / "requirements.lock").is_file():
        raise ValueError("Run install.sh from a complete codexd-bridge download")
    if layout.bin_dir == layout.root or layout.root in layout.bin_dir.parents:
        raise ValueError("Choose a command directory outside the application directory")
    with locked(layout.root), locked(layout.bin_dir / APP):
        previous = manifest(layout.root)
        previous_release = (layout.root / "current").resolve() if previous else None
        if previous and previous["bin_dir"] != str(layout.bin_dir):
            raise ValueError("Use the original --bin-dir when upgrading this installation")
        require_owned_link(layout.bin_dir / APP, layout.command)
        old_profiles = {entry["path"]: entry for entry in (previous or {}).get("profiles", [])}
        profile_snapshots(layout, shell)
        layout.root.mkdir(parents=True, exist_ok=True)
        releases = layout.root / "releases"
        releases.mkdir(exist_ok=True)
        release = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
        changes = FileChanges()
        try:
            print("Installing the application and its dependencies...", flush=True)
            build_release(source, release)
            # Dependency installation may take minutes. Re-read user files before publishing.
            profiles = profile_snapshots(layout, shell)
            require_owned_link(layout.bin_dir / APP, layout.command)
            command = ("#!/bin/sh\n"
                       f"CODEXD_BRIDGE_EXECUTABLE={shlex.quote(str(layout.command))}\n"
                       "export CODEXD_BRIDGE_EXECUTABLE\n"
                       f"exec {shlex.quote(str(layout.root / 'current/bin/python'))} "
                       '-I -m codexd_bridge.cli "$@"\n')
            changes.text(layout.command, command, 0o755)
            changes.text(layout.root / "manage_install.py", Path(__file__).read_text())
            for path, content, block, existed in profiles:
                inserted = block
                if block not in content:
                    separator = "" if not content or content.endswith("\n") else "\n"
                    inserted = separator + block
                    changes.text(path, content + inserted,
                                 path.stat().st_mode & 0o777 if existed else 0o644)
                old_profiles.setdefault(str(path), {"path": str(path), "block": inserted,
                                                    "created": not existed})
            changes.link(layout.root / "current", release)
            changes.link(layout.bin_dir / APP, layout.command)
            legacy = layout.bin_dir / (APP + "-uninstall")
            if legacy.is_symlink() and legacy.readlink() == layout.root / "bin" / legacy.name:
                changes.save(legacy)
                legacy.unlink()
            changes.text(layout.root / MARKER, json.dumps({
                "application": APP, "version": 1, "root": str(layout.root),
                "bin_dir": str(layout.bin_dir), "profiles": list(old_profiles.values()),
            }, indent=2) + "\n")
        except BaseException:
            changes.rollback()
            shutil.rmtree(release)
            if previous is None:
                shutil.rmtree(layout.root)
            raise
    print(f"Installed: {layout.bin_dir / APP}")
    if previous_release is not None and os.path.lexists(layout.root / "registrations.json"):
        subprocess.run([str(layout.root / "current/bin/python"), "-I", "-c",
                        "from pathlib import Path; import sys; "
                        "from codexd_bridge.installation import refresh_installation; "
                        "refresh_installation(Path(sys.argv[1]), Path(sys.argv[2]))",
                        str(layout.root), str(previous_release)], check=True)
    print("Open a new terminal to use codexd-bridge from any directory.")
    print("Uninstall at any time with codexd-bridge uninstall.")


def uninstall(root: Path):
    with locked(root):
        data = manifest(root)
        if data is None:
            print("Codex Daemon Bridge is not installed here.")
            return
        with locked(Path(data["bin_dir"]) / APP):
            _uninstall_owned(root, data)
    print("Codex Daemon Bridge and its commands were removed. Backend configuration, credentials and sessions were kept.")
    print("Owned Codex integrations were disabled. No model backend was stopped.")


def _uninstall_owned(root: Path, data: dict):
    if os.path.lexists(root / "registrations.json"):
        subprocess.run([str(root / "current/bin/python"), "-I", "-c",
                        "from pathlib import Path; import sys; "
                        "from codexd_bridge.installation import detach_installation; "
                        "detach_installation(Path(sys.argv[1]))", str(root)], check=True)
    bin_dir = Path(data["bin_dir"])
    for name in (APP, APP + "-uninstall"):
        link = bin_dir / name
        target = root / "bin" / name
        if link.is_symlink() and link.readlink() == target:
            link.unlink()
        elif os.path.lexists(link):
            print(f"Kept a command that no longer belongs to this installation: {link}")
    for entry in data.get("profiles", []):
        path, block = Path(entry["path"]), entry["block"]
        if path.is_symlink() or not path.is_file():
            continue
        content = read_text(path)
        if block not in content:
            continue
        updated = content.replace(block, "", 1)
        if not updated and entry["created"]:
            path.unlink()
        else:
            atomic_text(path, updated, path.stat().st_mode & 0o777)
    shutil.rmtree(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--prefix", type=Path, help="Application directory (not the provider data directory)")
    parser.add_argument("--bin-dir", type=Path, help="Command directory (default: ~/.local/bin)")
    parser.add_argument("--no-launch", action="store_true", help="Do not open the TUI after installation")
    args = parser.parse_args()
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share").expanduser()
    root = (args.prefix or data_home / APP / "program").expanduser().absolute()
    bin_dir = (args.bin_dir or home / ".local/bin").expanduser().absolute()
    try:
        if root.is_symlink():
            raise ValueError("Refusing a symbolic link as the application directory")
        root, bin_dir = root.resolve(), bin_dir.resolve()
        if root == home.resolve() or root == Path("/") or root in home.resolve().parents:
            raise ValueError("Choose a dedicated application directory")
        if args.action == "uninstall":
            uninstall(root)
            return
        if sys.platform != "linux":
            raise ValueError("This installer currently supports Linux (including WSL)")
        if args.source is None:
            raise ValueError("An installation source is required")
        layout = Layout(home, root, bin_dir)
        install(args.source.resolve(), layout, shell=os.environ.get("SHELL", "/bin/bash"))
        if not args.no_launch and sys.stdin.isatty() and sys.stdout.isatty():
            os.execv(str(layout.command), [str(layout.command), "ui"])
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"codexd-bridge installer: {error}\n")


if __name__ == "__main__":
    main()
