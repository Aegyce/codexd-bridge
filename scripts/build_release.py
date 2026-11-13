"""Build an audited sdist, then build the wheel from that exact sdist."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def project_version():
    tree = ast.parse((ROOT / "src/codexd_bridge/__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("Project version is missing")


def check_member(name):
    path = PurePosixPath(name)
    forbidden = {".git", ".venv", "tmp", "__pycache__", ".env", "auth.json", "sessions", "node_modules"}
    if path.is_absolute() or ".." in path.parts or forbidden.intersection(path.parts):
        raise ValueError(f"Unexpected release archive entry: {name}")


def unpack_source(archive: Path, destination: Path):
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            check_member(member.name)
            if not member.isfile() and not member.isdir():
                raise ValueError("Source archive must contain only ordinary files and directories")
        # Archive names and types have been checked; the destination is an empty private directory.
        source.extractall(destination, filter="data")
    directories = list(destination.iterdir())
    if len(directories) != 1 or not directories[0].is_dir():
        raise ValueError("Source archive must contain one project directory")
    project = directories[0]
    for name in ("install.sh", "uninstall.sh", "requirements.lock", "pyproject.toml", "README.md",
                 "README.zh-CN.md", "LICENSE", "COMMERCIAL-LICENSE.md", "scripts/manage_install.py",
                 "src/codexd_bridge/skill/SKILL.md"):
        if not (project / name).is_file():
            raise ValueError(f"Source archive is missing {name}")
    return project


def build(output: Path):
    version = project_version()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=output) as directory:
        stage = Path(directory).resolve()
        subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_sdist; "
                        "import sys; build_sdist(sys.argv[1])", str(stage)], cwd=ROOT, check=True)
        archive, = stage.glob("*.tar.gz")
        source = stage / "source"
        source.mkdir()
        project = unpack_source(archive, source)
        subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_wheel; "
                        "import sys; build_wheel(sys.argv[1])", str(stage)], cwd=project, check=True)
        wheel, = stage.glob("*.whl")
        with zipfile.ZipFile(wheel) as package:
            for name in package.namelist():
                check_member(name)
            prefix = f"codexd_bridge-{version}.dist-info/"
            metadata = package.read(prefix + "METADATA").decode()
            if f"Version: {version}\n" not in metadata or "Requires-Python: >=3.11\n" not in metadata:
                raise ValueError("Wheel metadata does not match this release")
            if "codexd-bridge = codexd_bridge.cli:main" not in package.read(prefix + "entry_points.txt").decode():
                raise ValueError("Wheel command entry point is missing")
            package.read("codexd_bridge/skill/SKILL.md")
            package.read("codexd_bridge/diagnostics.py")
        sums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                       for path in (archive, wheel))
        (stage / "SHA256SUMS").write_text(sums)
        (stage / "release-notes.txt").write_text((ROOT / "CHANGELOG.md").read_text())
        for path in (archive, wheel, stage / "SHA256SUMS", stage / "release-notes.txt"):
            os.replace(path, output / path.name)
    print(json.dumps({"version": version, "artifacts": str(output.resolve()),
                      "wheel_built_from_sdist": True, "published": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / ("release-v" + project_version()))
    build(parser.parse_args().output.resolve())
