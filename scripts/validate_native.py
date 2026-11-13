"""Release smoke using only disposable native daemons; no model inference or credentials."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

from codexd_bridge import __version__
from codexd_bridge.config import load_backends
from codexd_bridge.integration import BRIDGE_TOOLS, install_skill, register_bridge
from codexd_bridge.providers import _minimal_catalog, install_provider_launcher
from codexd_bridge.provisioning import attach_daemon, create_daemon, daemon_action
from codexd_bridge.sessions import validate_selection
from codexd_bridge.transport import AppServer


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fresh_session(home, root):
    with AppServer(home) as client:
        thread = client.call("thread/start", {"modelProvider": "openai", "model": "test-model",
            "cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only"})["thread"]
        validate_selection(client, {"model": "test-model", "effort": "none"})
        skills = client.call("skills/list", {"cwds": [str(root)], "forceReload": True})
        require(any(s["name"] == "codexd-bridge" and s["enabled"]
            for entry in skills["data"] for s in entry["skills"]), "Bundled skill not discovered")
        status = client.call("mcpServerStatus/list", {"serverName": "codexd-bridge", "limit": 1})
        require(set(BRIDGE_TOOLS).issubset({t["name"] for entry in status["data"]
            for t in entry["tools"].values()}), "MCP tool catalog is incomplete")
        return thread["id"]


def validate(root: Path, command: Path, upgrade_source: Path | None = None):
    # The parent has created a private, empty location. Never accept existing backend homes.
    if any((root / name).exists() for name in ("a", "b", "routes.toml")):
        raise ValueError("Native validation requires an unused scratch directory")
    registry = root / "routes.toml"
    routes = []

    def remove(name, *flags):
        result = subprocess.run([str(command), "--config", str(registry), "daemon", "remove", name,
            *flags], cwd="/", text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    try:
        for name in ("a", "b"):
            backend = create_daemon(name, root / name, "openai", "test-model", registry_path=registry,
                                    catalog_text=json.dumps(_minimal_catalog("test-model", None, 32768)))
            routes.append(backend)
            daemon_action(backend, "start")
            install_skill(backend.home)
            result = register_bridge(backend.home, command.resolve(), registry)
            require(result["new_sessions_ready"], "Native tool registration failed")
        a, b = routes
        first = fresh_session(a.home, root)
        daemon_action(a, "stop")
        daemon_action(a, "start")
        second = fresh_session(a.home, root)
        require(first != second, "Expected a fresh native session after restart")
        retained = fresh_session(b.home, root)
        if upgrade_source is not None:
            # Reproduce stale registration metadata, then exercise the distributed upgrade.
            with AppServer(b.home) as client:
                client.call("config/batchWrite", {"edits": [{"keyPath": "mcp_servers.codexd-bridge.env.CODEXD_BRIDGE_VERSION",
                    "value": "0.3.0", "mergeStrategy": "replace"}], "filePath": str(b.home / "config.toml"),
                    "reloadUserConfig": True})
            subprocess.run(["bash", str(upgrade_source / "install.sh"), "--prefix", str(command.resolve().parent.parent),
                            "--bin-dir", str(command.parent), "--no-launch"], cwd="/", check=True)
            config = tomllib.loads((b.home / "config.toml").read_text())
            require(config["mcp_servers"]["codexd-bridge"]["env"]["CODEXD_BRIDGE_VERSION"] == __version__,
                    "Upgrade did not refresh registered MCP version")
            fresh_session(b.home, root)
            shutil.rmtree(upgrade_source.parent)
        launcher = install_provider_launcher("a", registry_path=registry, launcher_dir=root / "launchers",
                                             executable=command.resolve())["launcher"]
        result = remove("a")
        require(result["removed"] and not result["data_deleted"] and a.home.exists(), "Default removal lost data")
        require(not Path(launcher).exists(), "Owned launcher was not removed")
        require(list(load_backends(registry)) == ["b"], "Removal affected other routes")
        attach_daemon("a", a.home, registry_path=registry)
        daemon_action(a, "start")
        require(remove("a", "--purge")["data_deleted"] and not a.home.exists(), "Explicit purge failed")
        subprocess.run([str(command), "uninstall"], cwd="/", check=True)
        require(not command.exists() and registry.is_file(), "Bridge uninstall scope is wrong")
        with AppServer(b.home) as client:
            thread = client.call("thread/read", {"threadId": retained, "includeTurns": False})["thread"]
            require(thread["id"] == retained, "Bridge uninstall affected native session data")
        config = tomllib.loads((b.home / "config.toml").read_text())
        require(config["mcp_servers"]["codexd-bridge"]["enabled"] is False, "Owned MCP not disabled")
        print(json.dumps({"native_discovery": True, "discovery_after_restart": True,
                          "upgrade_refreshed_registration": upgrade_source is not None,
                          "catalog_default_verified": True,
                          "daemon_remove_keep_and_purge": True, "other_session_retained": True,
                          "application_uninstall_preserved_daemon": True, "inference_run": False}))
    finally:
        # These are exclusively the daemons this process created; never enumerate user daemons.
        for backend in routes:
            if backend.home.exists():
                daemon_action(backend, "stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--upgrade-source", type=Path)
    args = parser.parse_args()
    validate(args.root.resolve(), args.command.absolute(), args.upgrade_source)
