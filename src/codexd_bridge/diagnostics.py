"""Read-only installation diagnostics; never make model requests or read credentials."""

from __future__ import annotations

import importlib.metadata
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import __version__
from .config import load_backends
from .providers import _resolve_executable
from .provisioning import _check_start_binding
from .transport import BridgeError


def diagnose(registry: Path) -> dict:
    checks = []

    def add(name, status, detail, advice=None):
        checks.append({"name": name, "status": status, "detail": detail, "advice": advice})

    add("Python", "ok" if sys.version_info >= (3, 11) else "error", sys.version.split()[0],
        None if sys.version_info >= (3, 11) else "Install Python 3.11 or newer.")
    for package, expected in (("mcp", "2.3.0"), ("websockets", "16.1.1"), ("prompt-toolkit", "3.0.53")):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            add(package, "error", "Not installed", "Run install.sh again to repair dependencies.")
        else:
            add(package, "ok" if version == expected else "warning", version,
                None if version == expected else f"This release was validated with {package} {expected}.")
    executable = shutil.which("codex")
    if executable is None:
        add("Codex CLI", "error", "Not found on PATH", "Install Codex CLI and reopen this terminal.")
    else:
        try:
            response = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
            version = re.fullmatch(r"codex-cli ([0-9][a-zA-Z0-9.+-]{0,63})\s*", response.stdout)
            if response.returncode != 0 or version is None:
                raise ValueError("unexpected version response")
            add("Codex CLI", "ok", version[1])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            add("Codex CLI", "error", "Could not verify the installed CLI",
                "Check that codex --version works in this terminal.")
    bridge = None
    try:
        bridge = _resolve_executable(None)
        add("Bridge command", "ok", str(bridge))
    except (BridgeError, OSError, ValueError):
        add("Bridge command", "error", "Installed entry point is unavailable",
            "Run install.sh again to restore the command.")
    if not registry.exists():
        add("Provider registry", "warning", "No configuration yet",
            "Open the manager and choose Set up this Codex or New provider.")
        routes = {}
    else:
        try:
            routes = load_backends(registry)
            add("Provider registry", "ok", f"{len(routes)} backend(s)")
        except (OSError, ValueError):
            add("Provider registry", "error", "Configuration could not be loaded",
                "Use More tools > Choose registry to select a valid registry, or restore this file.")
            routes = {}
    for name, backend in routes.items():
        try:
            _check_start_binding(backend, backend.home)
        except (OSError, ValueError, BridgeError):
            add(name, "error", "Backend directory or provider binding is invalid",
                "Select the correct Codex directory and provider in the manager.")
            continue
        detail = "Provider binding matches; " + ("route enabled" if backend.enabled else "route disabled")
        detail += "; " + ("control socket present" if backend.socket_path.exists() else "no control socket")
        add(name, "ok" if backend.enabled else "warning", detail,
            "Use Daemon status to check the process; Connect bridge verifies model access.")
        if bridge is not None:
            from .installation import owned_servers

            try:
                config = tomllib.loads((backend.home / "config.toml").read_text())
                entries = config.get("mcp_servers", {})
                stale = [key for key in owned_servers(config, bridge, registry)
                         if entries[key].get("enabled") is not False
                         and (not isinstance(entries[key].get("env"), dict)
                              or entries[key]["env"].get("CODEXD_BRIDGE_VERSION") != __version__)]
                if stale:
                    add(name + " MCP registration", "warning", "Registered version differs from this program",
                        "Use Connect bridge to refresh this backend, including compatibility entries.")
            except (OSError, ValueError, BridgeError):
                add(name + " MCP registration", "warning", "Could not inspect registration metadata",
                    "Inspect this backend through Connect bridge.")
    return {"version": __version__, "ok": not any(check["status"] == "error" for check in checks),
            "registry": str(registry), "checks": checks, "model_requests": False}


def inspect_codex(home: Path) -> dict:
    """Read non-secret state from an existing daemon; never start one or read auth files."""
    from .transport import AppServer

    home = home.expanduser()
    if not home.is_absolute():
        raise ValueError("Choose an absolute Codex directory.")
    if home.exists() and not home.is_dir():
        raise ValueError("Choose a Codex directory, not a file.")
    result = {"home": str(home), "connected": False, "provider": None, "model": None,
              "authentication": "unknown", "account": "unknown", "plan": None}
    if not (home / "app-server-control/app-server-control.sock").exists():
        return result
    with AppServer(home, timeout=5) as client:
        config = client.call("config/read", {"includeLayers": False}).get("config", {})
        response = client.call("account/read", {"refreshToken": False})
    account = response.get("account")
    account_type = account.get("type") if isinstance(account, dict) else None
    kind = account_type if account_type in {"apiKey", "chatgpt", "amazonBedrock", "gcp"} else "unknown"
    required = response.get("requiresOpenaiAuth")
    if required is True:
        authentication = "sign_in_required" if account is None else kind
    elif required is False:
        # A saved OpenAI login may be unrelated to a custom provider's credentials.
        authentication = kind if kind in {"amazonBedrock", "gcp"} else "provider_managed"
    else:
        authentication = "unknown"
    result.update(connected=True, authentication=authentication, account=kind)
    if isinstance(config, dict):
        for key, native in (("provider", "model_provider"), ("model", "model")):
            value = config.get(native)
            result[key] = value if isinstance(value, str) and value.strip() else None
    if kind == "chatgpt":
        plan = account.get("planType")
        if isinstance(plan, str) and plan != "unknown" and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", plan):
            result["plan"] = plan
    return result
