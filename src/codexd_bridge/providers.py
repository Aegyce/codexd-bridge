"""Provider onboarding (add_provider/connect_provider) and the codex-NAME launcher body (run_provider).

Reuses provisioning, probe and integration instead of repeating them. Routes are
created disabled and enabled only after real verification, integration and
launcher installation succeed. Only env-var names are stored, never values.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import (
    _BACKEND_NAME_RE,
    _MAX_TEXT_LENGTH,
    _expand_user,
    _resolve_config_path,
    load_backends,
)
from .integration import install_skill, register_bridge
from .probe import check_model
from .provisioning import (
    ProvisioningError,
    _base_url,
    _check_start_binding,
    _env_name,
    _load_registry,
    _text,
    _toml_dumps,
    create_daemon,
    daemon_action,
    set_backend_enabled,
)
from .transport import AppServer, BridgeError

__all__ = ["ProviderError", "add_provider", "connect_provider", "run_provider"]

DEFAULT_LAUNCHER_DIR = Path("~/.local/bin")
_DATA_BASE = Path("~/.local/share/codexd-bridge")
_LAUNCHER_PREFIX = "codex-"
_HELP_FLAGS = frozenset({"--help", "-h", "--version", "-V"})

# Native subcommands that attach to the owning daemon (verified --remote support).
_ATTACH = frozenset({"resume", "fork", "agents", "queue"})
# Non-interactive subcommands forwarded to codex unchanged, with no daemon.
_PASSTHROUGH = frozenset(
    {
        "exec", "e", "review", "help", "debug", "mcp", "plugin", "login", "logout",
        "app-server", "completion", "doctor", "features", "sandbox", "update",
        "cloud", "apply", "a", "exec-server", "remote-control", "migrate-rollouts",
        "migrate-nfs-db", "archive", "delete", "unarchive",
    }
)
_COMMANDS = _ATTACH | _PASSTHROUGH
_LONG_VALUE = frozenset(
    {"--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
     "--image", "--model", "--profile", "--sandbox", "--cd", "--local-provider",
     "--ask-for-approval", "--add-dir"}
)
_SHORT_VALUE = frozenset({"-c", "-m", "-p", "-s", "-C", "-i", "-a"})


class ProviderError(BridgeError):
    """Onboarding or launch failed; route state is preserved and no launcher is active.

    The optional keyword-only ``detail`` carries the cause text alone, without the
    retry-command advice, for structured UI feedback; ``str(error)`` keeps the full
    CLI wording. It is None when no cause was attached.
    """

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


def _progress_stage(progress: Callable[[str], None] | None, stage: str) -> None:
    """Report an onboarding stage on entry; silent when no callback was given."""
    if progress is not None:
        progress(stage)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _BACKEND_NAME_RE.fullmatch(name):
        raise ProvisioningError(
            "name must start with a lowercase ASCII letter and contain only "
            "lowercase ASCII letters, digits, '_' or '-'"
        )
    if len(name) > _MAX_TEXT_LENGTH:
        raise ProvisioningError(f"name must be at most {_MAX_TEXT_LENGTH} characters")
    return name


def _minimal_catalog(model: str, effort: str | None, context_window: int) -> dict:
    """Minimal legal native catalog for exactly the user's model (original instructions)."""
    entry: dict = {
        "slug": model,
        "display_name": model,
        "description": "User-declared model; only the declared context window and effort are advertised.",
        "base_instructions": "You are a helpful coding assistant. Follow the user's instructions.",
        "supported_reasoning_levels": (
            [{"effort": effort, "description": "User-specified reasoning effort"}] if effort else []
        ),
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "support_verbosity": False,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [],
        "context_window": context_window,
        "auto_compact_token_limit": context_window * 8 // 10,
    }
    if effort:
        entry["default_reasoning_level"] = effort
    return {"models": [entry]}


def _check_external_catalog(path: Path, model: str, context_window: int, compact: int) -> None:
    """The external catalog's selected model must use the declared bound and 80% compaction."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProvisioningError(f"model_catalog cannot be read as JSON: {error}") from None
    models = data.get("models") if isinstance(data, dict) else None
    entry = next(
        (m for m in models if isinstance(m, dict) and m.get("slug") == model), None
    ) if isinstance(models, list) else None
    if entry is None:
        raise ProvisioningError("model_catalog does not contain the selected model")
    if entry.get("context_window") != context_window or entry.get("auto_compact_token_limit") != compact:
        raise ProvisioningError(
            "model_catalog context_window/auto_compact_token_limit for the selected model "
            "must match the declared bound (80% compaction)"
        )


def _resolve_executable(executable: Path | None) -> Path:
    if executable is None:
        installed = os.environ.get("CODEXD_BRIDGE_EXECUTABLE")
        candidate = Path(sys.executable).parent / "codexd-bridge"
        executable = (Path(installed) if installed else candidate if candidate.is_file()
                      else Path(shutil.which("codexd-bridge") or ""))
    executable = Path(executable).expanduser()
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ProviderError("Provide an existing executable file at an absolute --executable path")
    return executable.resolve()


def _launcher_text(executable: Path, registry: Path, name: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(str(executable))} --config {shlex.quote(str(registry))} "
        f"provider run {shlex.quote(name)} \"$@\"\n"
    )


def _write_temp(directory: Path, text: str, mode: int) -> Path:
    fd, name = tempfile.mkstemp(dir=str(directory), prefix=".codexd-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        data = text.encode("utf-8")
        off = 0
        while off < len(data):
            off += os.write(fd, data[off:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise
    os.close(fd)
    return Path(name)


def _install_launcher(path: Path, text: str) -> tuple[int, int]:
    """Atomically create the launcher via temp+link; never overwrite. Returns its inode id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_temp(path.parent, text, 0o755)
    try:
        os.link(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


def _existing_launcher(path: Path, text: str) -> bool:
    if not os.path.lexists(path):
        return False
    if path.is_symlink() or not path.is_file():
        raise ProvisioningError(f"launcher {path.name!r} is not a regular file; refusing to overwrite")
    if path.read_text(encoding="utf-8") != text:
        raise ProvisioningError(f"launcher {path.name!r} has different content; refusing to overwrite")
    if not os.access(path, os.X_OK):
        raise ProvisioningError(f"launcher {path.name!r} is not executable; refusing to reuse")
    return True


def _remove_own_launcher(path: Path, ident: tuple[int, int], text: str) -> None:
    try:
        st = os.lstat(path)
        if (st.st_dev, st.st_ino) == ident and path.read_text(encoding="utf-8") == text:
            os.unlink(path)
    except (OSError, UnicodeError):
        pass


def install_provider_launcher(name: str, *, launcher_dir: Path | None = None,
                              executable: Path | None = None, registry_path: Path | None = None) -> dict:
    """Install the command for an existing route without probing or starting its daemon."""
    from .installation import installation_operation, remember_registration

    name = _validate_name(name)
    registry = _resolve_config_path(registry_path)[0].absolute()
    routes = load_backends(registry)
    if name not in routes:
        raise ProviderError(f"backend {name!r} not found; create or attach it first")
    executable = _resolve_executable(executable)
    directory = _expand_user(Path(launcher_dir) if launcher_dir is not None else DEFAULT_LAUNCHER_DIR,
                             "launcher_dir")
    if not directory.is_absolute():
        raise ProvisioningError("launcher_dir must be an absolute path after '~' expansion")
    launcher = directory / f"{_LAUNCHER_PREFIX}{name}"
    text = _launcher_text(executable, registry, name)
    ident = None
    with installation_operation(executable):
        try:
            if not _existing_launcher(launcher, text):
                ident = _install_launcher(launcher, text)
            remember_registration(routes[name].home, executable, registry, launcher=launcher)
        except BaseException:
            if ident is not None:
                _remove_own_launcher(launcher, ident, text)
            raise
        return {"backend": name, "launcher": str(launcher), "created": ident is not None}


def _ensure_running(backend, timeout: float) -> dict:
    """Reuse a running daemon, start a stopped one once; require 'running', never restart."""
    _check_start_binding(backend, backend.home)
    status = daemon_action(backend, "status", timeout=timeout)
    state = status.get("status")
    if state == "stopped":
        daemon_action(backend, "start", timeout=timeout)
        status = daemon_action(backend, "status", timeout=timeout)
        state = status.get("status")
    if state != "running":
        if state == "unreachable":
            raise ProviderError(
                f"backend {backend.name!r}: daemon is unreachable; inspect or stop it manually "
                "(no automatic restart)"
            )
        raise ProviderError(
            f"backend {backend.name!r}: daemon is not running (status {state!r}); "
            "query 'server status' before retrying"
        )
    return status


def _retry_command(name, registry, launcher_dir_path, executable_path, update_skill=False) -> str:
    # --config is a global CLI flag and must precede the 'provider connect' subcommand.
    parts = ["codexd-bridge"]
    if registry != _resolve_config_path(None)[0].absolute():
        parts += ["--config", str(registry)]
    parts += ["provider", "connect", name]
    if update_skill:
        parts.append("--update-skill")
    if launcher_dir_path != _expand_user(DEFAULT_LAUNCHER_DIR, "launcher_dir"):
        parts += ["--launcher-dir", str(launcher_dir_path)]
    try:
        default_exe = _resolve_executable(None)
    except ProviderError:
        default_exe = None
    if executable_path != default_exe:
        parts += ["--executable", str(executable_path)]
    return " ".join(shlex.quote(part) for part in parts)


def connect_provider(
    name: str,
    *,
    launcher_dir: Path | None = None,
    executable: Path | None = None,
    registry_path: Path | None = None,
    timeout: float = 60,
    update_skill: bool = False,
    client_factory=AppServer,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Verify, integrate, enable and install the launcher for an existing route (retry-safe).

    Real-checks the model, installs the skill, registers MCP, installs the launcher
    and enables the route only on full success. On failure the route's enabled state
    is preserved and no new launcher is left. An identical existing launcher is kept;
    a differing one is refused. Works with non-default registry/launcher/executable.

    With ``progress``, reports each stage on entry: 'daemon', 'verification',
    'integration', 'launcher', and 'ready' only after full success.
    """
    name = _validate_name(name)
    registry = _resolve_config_path(registry_path)[0].absolute()
    backends = load_backends(registry)
    if name not in backends:
        raise ProviderError(f"backend {name!r} not found; create it first")
    backend = backends[name]
    executable_path = _resolve_executable(executable)
    launcher_dir_path = _expand_user(
        Path(launcher_dir) if launcher_dir is not None else DEFAULT_LAUNCHER_DIR, "launcher_dir"
    )
    if not launcher_dir_path.is_absolute():
        raise ProvisioningError("launcher_dir must be an absolute path after '~' expansion")
    launcher_path = launcher_dir_path / f"{_LAUNCHER_PREFIX}{name}"
    text = _launcher_text(executable_path, registry, name)
    _existing_launcher(launcher_path, text)
    ident = None
    try:
        _progress_stage(progress, "daemon")
        _ensure_running(backend, timeout)
        _progress_stage(progress, "verification")
        verification = check_model(backend, timeout=timeout, client_factory=client_factory)
        _progress_stage(progress, "integration")
        skill = install_skill(backend.home, update=update_skill)
        registration = register_bridge(
            backend.home, executable_path, registry, client_factory=client_factory
        )
        if registration.get("effective") is False:
            raise ProviderError(
                "MCP registration is not effective; "
                + str(registration.get("warning") or "check tool discovery and configuration policy")
            )
        from .installation import installation_operation, remember_registration

        with installation_operation(executable_path):
            _progress_stage(progress, "launcher")
            # Verification can take time; the preflight result is no longer current.
            if not _existing_launcher(launcher_path, text):
                ident = _install_launcher(launcher_path, text)
            remember_registration(backend.home, executable_path, registry, launcher=launcher_path)
            set_backend_enabled(name, True, registry_path=registry, expected_backend=backend)
    except BaseException as error:
        if ident is not None:
            _remove_own_launcher(launcher_path, ident, text)
        if isinstance(error, (BridgeError, ValueError, OSError)):
            raise ProviderError(
                f"provider {name!r} connect failed; route state preserved and no new launcher "
                f"installed. Retry with: {_retry_command(name, registry, launcher_dir_path, executable_path, update_skill)}. "
                f"If the connection settings are wrong, correct {backend.home / 'config.toml'} before retrying. "
                f"Cause: {error}",
                detail=str(error),
            ) from error
        raise
    _progress_stage(progress, "ready")
    return {
        "backend": name,
        "home": str(backend.home),
        "launcher": str(launcher_path),
        "skill": str(skill),
        "enabled": True,
        "verification": verification,
        "registration": registration,
    }


def add_provider(
    name: str,
    base_url: str,
    env_key: str,
    model: str,
    context_window: int,
    *,
    effort: str | None = None,
    model_catalog: Path | None = None,
    home: Path | None = None,
    launcher_dir: Path | None = None,
    executable: Path | None = None,
    registry_path: Path | None = None,
    wire_api: str = "responses",
    timeout: float = 60,
    client_factory=AppServer,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Create a disabled route from connection settings, then connect_provider it.

    Stores only the env-var name. Generates a minimal catalog inside the new home
    (home/model-catalog.json) unless an external catalog is referenced; an external
    catalog must already declare the selected model with the same context window and
    80% compaction. Existing routes/homes/launchers (incl. dangling symlinks) are refused.

    With ``progress``, reports 'configuration' after validation just before home
    creation, then forwards the callback to connect_provider for its stages.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, int | float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ProvisioningError("timeout must be a positive finite number of seconds")
    name = _validate_name(name)
    base_url = _base_url(base_url, f"provider {name!r}")
    env_key = _env_name(env_key, f"provider {name!r}: field 'env_key'")
    model = _text(model, "model")
    effort = None if effort is None else _text(effort, "effort")
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window < 1:
        raise ProvisioningError("context_window must be a positive integer; a finite bound is required")
    if wire_api != "responses":
        raise ProvisioningError("wire_api must be 'responses'; this Codex version no longer supports Chat Completions")
    compact = context_window * 8 // 10

    registry = _resolve_config_path(registry_path)[0].absolute()
    if name in _load_registry(registry)["backends"]:
        raise ProvisioningError(f"backend {name!r} already exists in the registry; refusing to overwrite")

    home_path = (
        _expand_user(_DATA_BASE / "homes" / name, "home")
        if home is None
        else _expand_user(Path(home), "home")
    )
    if not home_path.is_absolute():
        raise ProvisioningError("home must be an absolute path after '~' expansion")
    if os.path.lexists(home_path):
        raise ProvisioningError("home already exists (symlinks included); refusing to overwrite")

    launcher_dir_path = _expand_user(
        Path(launcher_dir) if launcher_dir is not None else DEFAULT_LAUNCHER_DIR, "launcher_dir"
    )
    if not launcher_dir_path.is_absolute():
        raise ProvisioningError("launcher_dir must be an absolute path after '~' expansion")
    launcher_path = launcher_dir_path / f"{_LAUNCHER_PREFIX}{name}"
    if os.path.lexists(launcher_path):
        raise ProvisioningError(
            f"launcher {launcher_path.name!r} already exists (symlinks included); refusing to overwrite"
        )
    executable_path = _resolve_executable(executable)

    if model_catalog is None:
        catalog_path = home_path / "model-catalog.json"
        catalog_text = json.dumps(_minimal_catalog(model, effort, context_window), ensure_ascii=False, indent=2) + "\n"
    else:
        catalog_path = _expand_user(Path(model_catalog), "model_catalog")
        if not catalog_path.is_absolute():
            raise ProvisioningError("model_catalog must be an absolute path after '~' expansion")
        if not catalog_path.is_file():
            raise ProvisioningError("model_catalog must reference an existing catalog file")
        _check_external_catalog(catalog_path, model, context_window, compact)
        catalog_text = None

    if home is None:
        home_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    template_config: dict = {
        "model_providers": {
            name: {"name": name, "base_url": base_url, "env_key": env_key, "wire_api": wire_api}
        }
    }
    if catalog_text is None:  # external reference only; generated catalog is written by provisioning
        template_config["model_catalog_json"] = str(catalog_path)
    with tempfile.TemporaryDirectory(prefix="codexd-provider-") as tmp:
        template = Path(tmp)
        (template / "config.toml").write_text(_toml_dumps(template_config), encoding="utf-8")
        _progress_stage(progress, "configuration")
        create_daemon(
            name, home_path, name, model,
            registry_path=registry, template_home=template, effort=effort, catalog_text=catalog_text,
        )

    result = connect_provider(
        name, launcher_dir=launcher_dir_path, executable=executable_path,
        registry_path=registry, timeout=timeout, client_factory=client_factory,
        progress=progress,
    )
    return {
        **result,
        "model_provider": name,
        "model": model,
        "effort": effort,
        "context_window": context_window,
        "auto_compact_token_limit": compact,
        "catalog": str(catalog_path),
        "registry": str(registry),
    }


def _first_positional(argv: list[str]) -> str | None:
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            return None  # Following words are prompt text, even when they name a command.
        if not tok.startswith("-") or tok == "-":
            return tok
        if tok.startswith("--"):
            i += 1 if ("=" in tok or tok not in _LONG_VALUE) else 2
        elif tok in _SHORT_VALUE:
            i += 2
        else:
            i += 1
        continue
    return None


def _leading_subcommand(argv: list[str]) -> str | None:
    tok = _first_positional(argv)
    return tok if tok in _COMMANDS else None


def _enforce_binding(argv: list[str], socket_url: str) -> bool:
    """Reject provider/profile/OSS overrides and --remote to other endpoints; return has_remote."""
    has_remote = False
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            break
        if tok == "--remote" or tok.startswith("--remote="):
            if "=" in tok:
                val, i = tok.split("=", 1)[1], i + 1
            else:
                val, i = (argv[i + 1] if i + 1 < n else None), i + 2
            if val != socket_url:
                raise ProviderError("refusing --remote to a different endpoint; this entry is bound to its own route")
            has_remote = True
            continue
        if tok in ("--profile", "-p") or tok.startswith("--profile=") or (tok.startswith("-p") and len(tok) > 2):
            raise ProviderError("refusing --profile override; provider binding is fixed")
        if tok == "--oss" or tok == "--local-provider" or tok.startswith("--local-provider="):
            raise ProviderError("refusing OSS/local-provider override; provider binding is fixed")
        if tok in ("-c", "--config") or tok.startswith("--config=") or (tok.startswith("-c") and len(tok) > 2):
            if tok.startswith("--config="):
                val, i = tok.split("=", 1)[1], i + 1
            elif tok.startswith("-c") and len(tok) > 2:
                val, i = tok[2:], i + 1
            else:
                val, i = (argv[i + 1] if i + 1 < n else ""), i + 2
            key = val.split("=", 1)[0].strip()
            if key.split(".", 1)[0] in ("model_provider", "model_providers"):
                raise ProviderError("refusing model_provider override; provider binding is fixed")
            continue
        i += 1
    return has_remote


def _server_action(backend, rest: list[str], timeout: float) -> dict:
    if rest in ([], ["help"], ["-h"], ["--help"]):
        return {"backend": backend.name, "usage": "server start|status|stop"}
    if len(rest) != 1 or rest[0] not in ("start", "status", "stop"):
        raise ProviderError("usage: server start|status|stop (restart is not supported)")
    if rest[0] == "start":
        return _ensure_running(backend, timeout)
    return daemon_action(backend, rest[0], timeout=timeout)


def run_provider(
    name: str,
    args: Sequence[str] = (),
    *,
    registry_path: Path | None = None,
    timeout: float = 45,
    exec_runner=os.execvpe,
) -> dict | None:
    """Run native codex bound to one route; preserve args/exit status/provider binding.

    'server start|status|stop' maps to the lifecycle. Interactive TUI and
    resume/fork/agents/queue attach to the route's own daemon via --remote; other
    subcommands, help and explicit --no-daemon mode pass through natively.
    Provider/profile/OSS overrides and
    --remote to other endpoints are refused. No automatic restart.
    """
    argv = list(args)
    registry = _resolve_config_path(registry_path)[0].absolute()
    backends = load_backends(registry)
    if name not in backends:
        raise ProviderError(f"backend {name!r} not found; run 'daemon list' to inspect routes")
    backend = backends[name]
    if argv[:1] == ["server"]:
        return _server_action(backend, argv[1:], timeout)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(backend.home)
    socket_url = "unix://" + str(backend.socket_path)
    has_remote = _enforce_binding(argv, socket_url)
    sub = _leading_subcommand(argv)
    options = argv[:argv.index("--")] if "--" in argv else argv
    standalone = "--no-daemon" in options
    if standalone or any(a in _HELP_FLAGS for a in options) or (sub is not None and sub not in _ATTACH):
        if standalone or sub in ("exec", "e", "review"):
            _check_start_binding(backend, backend.home)
        return exec_runner("codex", ["codex", *argv], env)
    _ensure_running(backend, timeout)
    connection = [] if has_remote else ["--remote", socket_url]
    return exec_runner("codex", ["codex", *connection, *argv], env)
