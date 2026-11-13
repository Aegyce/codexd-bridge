"""Install the release source archive, upgrade it, and uninstall from outside the source."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_release import ROOT, project_version, unpack_source


def validate_launcher_arguments(root, command, env):
    home, stub = root / "launcher-home", root / "native-stub"
    home.mkdir()
    stub.mkdir()
    (home / "config.toml").write_text('model_provider="demo"\n')
    registry = root / "launcher-routes.toml"
    registry.write_text('[backends.args]\nmodel_provider="demo"\nhome=' + json.dumps(str(home)) + '\n')
    native = stub / "codex"
    native.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                     'args = sys.argv[1:]\n'
                     'if args == ["app-server", "daemon", "version"]:\n'
                     '    print(json.dumps({"status": "running"}))\n'
                     'else:\n'
                     '    print(json.dumps({"argv": args, "home": os.environ["CODEX_HOME"]}))\n')
    native.chmod(0o755)
    env = {**env, "PATH": str(stub) + os.pathsep + env["PATH"]}
    result = subprocess.run([str(command), "--config", str(registry), "provider", "launcher", "args",
        "--launcher-dir", str(root / "arg-bin")], env=env, cwd="/", text=True, capture_output=True, check=True)
    launcher = json.loads(result.stdout)["launcher"]
    for args, attaches in ((["--", "exec"], True), (["--", "--help"], True),
                           (["--", "--no-daemon"], True), (["--no-daemon", "task"], False),
                           (["--ask-for-approval", "never", "exec", "task"], False)):
        result = subprocess.run([launcher, *args], env=env, cwd="/", text=True, capture_output=True, check=True)
        expected = (["--remote", "unix://" + str(home / "app-server-control/app-server-control.sock")]
                    if attaches else []) + args
        if json.loads(result.stdout) != {"argv": expected, "home": str(home)}:
            raise RuntimeError("Installed provider launcher changed native arguments or the selected home")
    print("PASS: installed provider launcher preserves native arguments", flush=True)


def validate(artifacts: Path, native: bool):
    archives = list(artifacts.glob("codexd_bridge-*.tar.gz"))
    if len(archives) != 1:
        raise ValueError("Select a release directory containing exactly one source archive")
    (ROOT / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="rel-", dir=ROOT / "tmp"))
    print("Validation directory: " + str(root), flush=True)
    user = root / "user"
    user.mkdir()
    profile = user / ".bashrc"
    profile.write_bytes(b"# User-owned settings without final newline")
    before = profile.read_bytes()
    source = root / "source"
    source.mkdir()
    project = unpack_source(archives[0], source)
    program, bin_dir = root / "program", root / "bin"
    command = bin_dir / "codexd-bridge"
    alias = root / "alias"
    alias.symlink_to(root, target_is_directory=True)
    env = {key: value for key, value in os.environ.items()
           if key not in ("CODEX_HOME", "CODEXD_BRIDGE_CONFIG", "CODEXD_BRIDGE_EXECUTABLE", "PYTHONPATH",
                          "ZDOTDIR", "XDG_CONFIG_HOME")}
    env.update(HOME=str(user), SHELL="/bin/bash", CODEXD_BRIDGE_PYTHON=sys.executable,
               PIP_CACHE_DIR=os.environ.get("PIP_CACHE_DIR", str(Path.home() / ".cache/pip")))
    args = ["bash", str(project / "install.sh"), "--prefix", str(alias / "user/../program"),
            "--bin-dir", str(alias / "bin"), "--no-launch"]
    subprocess.run(args, env=env, cwd="/", check=True)
    previous = (program / "current").resolve()
    subprocess.run(args, env=env, cwd="/", check=True)
    if previous == (program / "current").resolve() or not previous.exists():
        raise RuntimeError("Upgrade did not switch environments while retaining the previous one")
    if not native:
        shutil.rmtree(source)
    subprocess.run([str(command), "--version"], env=env, cwd="/", check=True)
    validate_launcher_arguments(root, command, env)
    if native:
        subprocess.run([str(program / "current/bin/python"), "-I", str(ROOT / "scripts/validate_sessions.py"),
                        "--root", str(root / "sessions"), "--command", str(command)], env=env, cwd="/", check=True)
        subprocess.run([str(program / "current/bin/python"), "-I", str(ROOT / "scripts/validate_native.py"),
                        "--root", str(root), "--command", str(command), "--upgrade-source", str(project)],
                       env=env, cwd="/", check=True)
    else:
        subprocess.run([str(command), "uninstall"], env=env, cwd="/", check=True)
    if program.exists() or os.path.lexists(command) or profile.read_bytes() != before:
        raise RuntimeError("Uninstall did not restore the user's command and shell state")
    print("PASS: source archive installation, upgrade, independent command, uninstall and shell preservation", flush=True)
    shutil.rmtree(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "tmp" / ("release-v" + project_version()))
    parser.add_argument("--native", action="store_true", help="Also validate native discovery and daemon removal; requires codex")
    args = parser.parse_args()
    validate(args.artifacts.resolve(), args.native)
