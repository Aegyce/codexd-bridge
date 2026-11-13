"""Provision isolated Codex daemon homes and control their lifecycle.

'create_daemon' generates a fresh CODEX_HOME (configuration only) and registers
it in the bridge registry; it never starts a daemon and never overwrites an
existing home. New routes are registered disabled (enabled = false) pending
model-availability verification; 'set_backend_enabled' flips that flag under
the same lock. 'daemon_action' routes start/status/stop to
'codex app-server daemon start|version|stop' with only CODEX_HOME overridden;
there is no automatic restart.

Security discipline (stdlib-only implementation):

* Only config.toml files are ever read. auth.json, credential files, session
  history and environment variable *values* are never read; env_key and
  env_http_headers are copied as variable-name references only.
* Template copying is allow-list based. Unknown provider fields or literal
  secret shapes are refused; messages name fields, never values.
* The registry is updated under flock with an atomic replace; other backends
  are preserved and a failed update leaves the file byte-identical.
* Errors surface as ProvisioningError (a ValueError), ConfigError (also a
  ValueError) or BridgeError so callers can catch them uniformly.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

# Private helpers are reused deliberately so registry resolution, '~' handling
# and backend naming stay identical to config.py (no second source of truth).
from .config import (
    _BACKEND_NAME_RE,
    _MAX_TEXT_LENGTH,
    Backend,
    _expand_user,
    _resolve_config_path,
    load_backends,
)
from .transport import BridgeError

__all__ = [
    "ProvisioningError",
    "create_daemon",
    "daemon_action",
    "set_backend_enabled",
]

_BUILTIN_PROVIDERS = frozenset({"openai"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_WIRE_APIS = frozenset({"chat", "responses"})

# Vetted, non-secret provider connection fields. Anything else in a template
# provider table (api_key, http_headers, ...) is refused, never copied.
_PROVIDER_FIELD_ORDER = (
    "name",
    "base_url",
    "env_key",
    "wire_api",
    "requires_openai_auth",
    "env_http_headers",
    "request_max_retries",
    "stream_max_retries",
    "stream_idle_timeout_ms",
)
_PROVIDER_FIELDS = frozenset(_PROVIDER_FIELD_ORDER)
_PROVIDER_INT_MINIMUMS = {
    "request_max_retries": 0,
    "stream_max_retries": 0,
    "stream_idle_timeout_ms": 1,
}

_DAEMON_SUBCOMMANDS = {"start": "start", "status": "version", "stop": "stop"}


class ProvisioningError(ValueError):
    """Invalid provisioning request, template or registry state.

    Catchable as ValueError. Messages name identifiers (backend, provider or
    field names) but never echo configuration values, endpoints or secrets.
    """


# ---------------------------------------------------------------------------
# Small stdlib TOML writer (str/bool/int scalars and nested tables only).
# ---------------------------------------------------------------------------


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ProvisioningError("internal error: unsupported TOML value type")


def _toml_dumps(data: dict) -> str:
    """Serialize the constrained schema and verify the round-trip before use."""
    lines: list[str] = []

    def emit(table: dict, path: tuple[str, ...]) -> None:
        if path:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, path + (key,))

    emit(data, ())
    text = "\n".join(lines) + "\n"
    if tomllib.loads(text) != data:
        raise ProvisioningError("internal error: TOML round-trip verification failed")
    return text


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def _atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Same-directory temp file + fsync + os.replace; never a partial write."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        # Cleanup cannot turn a committed registration into a failed one.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Validation helpers (all run before any filesystem side effect).
# ---------------------------------------------------------------------------


def _text(value: object, field: str, *, limit: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ProvisioningError(f"{field} must be at most {limit} characters")
    if "\x00" in value:
        raise ProvisioningError(f"{field} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ProvisioningError(f"{field} contains characters that are not valid UTF-8") from None
    return value


def _env_name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not _ENV_NAME_RE.fullmatch(value)
        or len(value) > _MAX_TEXT_LENGTH
    ):
        raise ProvisioningError(
            f"{field} must be an environment variable NAME (identifier syntax); "
            "literal secret values are never copied"
        )
    return value


def _base_url(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise ProvisioningError(f"{context}: field 'base_url' must be a non-empty URL string")
    if len(value) > 2048:
        raise ProvisioningError(f"{context}: field 'base_url' is too long")
    try:
        parts = urlsplit(value)
        userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        raise ProvisioningError(f"{context}: field 'base_url' is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProvisioningError(f"{context}: field 'base_url' must be an http(s) URL")
    if userinfo:
        raise ProvisioningError(f"{context}: field 'base_url' must not embed credentials")
    if parts.query or parts.fragment:
        raise ProvisioningError(
            f"{context}: field 'base_url' must not contain a query string or fragment"
        )
    return value


def _copy_provider(provider: str, definition: object) -> dict:
    """Copy only vetted non-secret fields from one template provider table."""
    context = f"template provider {provider!r}"
    if not isinstance(definition, dict):
        raise ProvisioningError(f"{context}: definition must be a table")
    unknown = sorted(set(definition) - _PROVIDER_FIELDS)
    if unknown:
        listed = ", ".join(repr(field) for field in unknown)
        raise ProvisioningError(
            f"{context}: unsupported field(s) {listed}; only vetted non-secret connection "
            "fields are copied and literal credentials are never allowed"
        )
    copied: dict = {}
    for field in _PROVIDER_FIELD_ORDER:
        if field not in definition:
            continue
        value = definition[field]
        if field == "name":
            copied[field] = _text(value, f"{context}: field 'name'")
        elif field == "base_url":
            copied[field] = _base_url(value, context)
        elif field == "env_key":
            copied[field] = _env_name(value, f"{context}: field 'env_key'")
        elif field == "wire_api":
            if not isinstance(value, str) or value not in _WIRE_APIS:
                raise ProvisioningError(
                    f"{context}: field 'wire_api' must be 'chat' or 'responses'"
                )
            copied[field] = value
        elif field == "requires_openai_auth":
            if not isinstance(value, bool):
                raise ProvisioningError(
                    f"{context}: field 'requires_openai_auth' must be a boolean"
                )
            copied[field] = value
        elif field == "env_http_headers":
            if not isinstance(value, dict):
                raise ProvisioningError(f"{context}: field 'env_http_headers' must be a table")
            headers: dict = {}
            for header, env_name in value.items():
                if not isinstance(header, str) or not _HEADER_NAME_RE.fullmatch(header):
                    raise ProvisioningError(
                        f"{context}: 'env_http_headers' keys must be HTTP header names"
                    )
                headers[header] = _env_name(env_name, f"{context}: 'env_http_headers' value")
            copied[field] = headers
        else:
            minimum = _PROVIDER_INT_MINIMUMS[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ProvisioningError(
                    f"{context}: field {field!r} must be an integer >= {minimum}"
                )
            copied[field] = value
    if provider not in _BUILTIN_PROVIDERS:
        for required in ("name", "base_url"):
            if required not in copied:
                raise ProvisioningError(
                    f"{context}: field {required!r} is required for non-built-in providers"
                )
    return copied


def _catalog_reference(value: object, template: Path) -> str:
    """Copy the model_catalog_json path reference only; never read or copy it."""
    field = "template field 'model_catalog_json'"
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningError(f"{field} must be a non-empty path string")
    if len(value) > 4096 or "\x00" in value:
        raise ProvisioningError(f"{field} is not a usable filesystem path")
    if value.lstrip().startswith(("{", "[")):
        raise ProvisioningError(f"{field} must be a path reference, not inline JSON")
    path = _expand_user(Path(value), field)
    if not path.is_absolute():
        # Keep the reference pointing at the template's catalog file.
        path = template / path
    return str(path)


def _template_settings(template: Path | None, provider: str) -> tuple[dict | None, str | None]:
    """Read only template/config.toml; return (provider copy, catalog reference)."""
    if template is None:
        if provider not in _BUILTIN_PROVIDERS:
            raise ProvisioningError(
                f"provider {provider!r} is not built in; template_home with a "
                f"[model_providers.{provider}] definition is required "
                "(endpoints are never guessed)"
            )
        return None, None
    config_path = template / "config.toml"
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ProvisioningError("template_home does not contain config.toml") from None
    except OSError:
        raise ProvisioningError("template config.toml cannot be read") from None
    except tomllib.TOMLDecodeError:
        # Decoder messages may quote configuration fragments; suppress them.
        raise ProvisioningError("template config.toml is not valid TOML") from None
    providers = data.get("model_providers", {})
    if not isinstance(providers, dict):
        raise ProvisioningError("template field 'model_providers' must be a table")
    definition = providers.get(provider)
    copied = None
    if definition is not None:
        copied = _copy_provider(provider, definition)
    elif provider not in _BUILTIN_PROVIDERS:
        raise ProvisioningError(
            f"template config.toml has no [model_providers.{provider}] definition; "
            "endpoints are never guessed"
        )
    catalog = data.get("model_catalog_json")
    catalog_path = None if catalog is None else _catalog_reference(catalog, template)
    return copied, catalog_path


# ---------------------------------------------------------------------------
# Registry (bridge backends configuration only).
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _registry_lock(registry: Path):
    """Serialize registry read-modify-write across processes and threads."""
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = registry.with_name(registry.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load_registry(registry: Path) -> dict[str, dict]:
    """Validate strictly via config.load_backends, then return raw entries.

    Raw entries are preserved verbatim on write, so other backends' fields
    (including 'enabled', or its absence in old manual configs) never change.
    """
    if not os.path.lexists(registry):
        return {"backends": {}}
    if registry.is_symlink() or not registry.is_file():
        raise ProvisioningError("registry path must be a regular file (symlinks are refused)")
    # Strict validation: an invalid registry is refused, never silently rewritten.
    load_backends(registry)
    try:
        with registry.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError:
        raise ProvisioningError("registry cannot be read") from None
    except tomllib.TOMLDecodeError:
        raise ProvisioningError("registry is not valid TOML") from None
    return data


def _backend_from_entry(name: str, entry: dict) -> Backend:
    return Backend(
        name=name,
        home=Path(entry["home"]).expanduser(),
        model_provider=entry["model_provider"],
        default_model=entry.get("default_model"),
        default_effort=entry.get("default_effort"),
        enabled=entry.get("enabled", True),
    )


def _register(registry: Path, name: str, entry: dict) -> None:
    with _registry_lock(registry):
        data = _load_registry(registry)
        existing = data["backends"]
        if name in existing:
            raise ProvisioningError(
                f"backend {name!r} already exists in the registry; refusing to overwrite"
            )
        merged = {**existing, name: entry}
        _atomic_write(registry, _toml_dumps({**data, "backends": merged}))


def _rollback_home(home: Path) -> None:
    """Best-effort removal of only what this call created inside a fresh home."""
    for name in ("config.toml", "model-catalog.json"):
        try:
            (home / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        home.rmdir()
    except OSError:
        pass


def _write_private_config(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def create_daemon(
    name: str,
    home: Path,
    model_provider: str,
    model: str,
    *,
    registry_path: Path | None = None,
    template_home: Path | None = None,
    effort: str | None = None,
    catalog_text: str | None = None,
) -> Backend:
    """Create a fresh isolated daemon home and register it disabled; never start it.

    'home' must be absolute (after '~' expansion) and must not exist in any
    form, including dangling symlinks; its parent must already exist. The new
    home is created 0700 with a 0600 config.toml binding model_provider
    explicitly to the requested value, plus approval_policy = "never" and
    sandbox_mode = "workspace-write". Sessions, state and the control socket
    are independent because nothing is shared or symlinked from the template.

    The route is registered with enabled = false, pending the caller's own
    model-availability verification; this function performs no inference and
    never starts a daemon. Use set_backend_enabled to enable it afterwards.

    'registry_path' defaults to the same resolution as config.load_backends
    (explicit argument, then CODEXD_BRIDGE_CONFIG, then
    ~/.config/codexd-bridge/config.toml). Only the bridge registry is modified,
    under flock with an atomic replace; existing backends are preserved verbatim
    (including their 'enabled' values) and an existing name is refused. The
    registry may not live inside the new home or point at the template's
    config.toml.

    Non-built-in providers require 'template_home' whose config.toml defines
    [model_providers.<provider>]; only vetted non-secret fields are copied
    (name/base_url/env_key/wire_api/requires_openai_auth/env_http_headers and
    request retry/timeout integers). model_catalog_json is copied as a path
    reference (relative paths resolve against the template home); the file
    itself is never read or copied. Unknown fields or literal-secret shapes
    are refused without echoing values. auth.json and environment variable
    values are never read.

    Existing route syntax is validated without requiring those daemons to be
    available. On registration failure the newly created home is rolled back.
    """
    if not isinstance(name, str) or not _BACKEND_NAME_RE.fullmatch(name):
        raise ProvisioningError(
            "name must start with a lowercase ASCII letter and contain only "
            "lowercase ASCII letters, digits, '_' or '-'"
        )
    if len(name) > _MAX_TEXT_LENGTH:
        raise ProvisioningError(f"name must be at most {_MAX_TEXT_LENGTH} characters")
    provider = _text(model_provider, "model_provider")
    model_name = _text(model, "model")
    effort_value = None if effort is None else _text(effort, "effort")

    if not isinstance(home, str | os.PathLike):
        raise ProvisioningError("home must be a path")
    home_path = _expand_user(Path(home), "home")
    if not home_path.is_absolute():
        raise ProvisioningError("home must be an absolute path after '~' expansion")
    if "\x00" in str(home_path):
        raise ProvisioningError("home must not contain NUL characters")
    if os.path.lexists(home_path):
        raise ProvisioningError(
            "home already exists (directories, files and symlinks are all refused); "
            "nothing is overwritten"
        )
    if not home_path.parent.is_dir():
        raise ProvisioningError("home parent directory must already exist")

    registry, _source = _resolve_config_path(registry_path)
    home_resolved = home_path.resolve()
    registry_resolved = registry.resolve()
    if registry_resolved == home_resolved or home_resolved in registry_resolved.parents:
        raise ProvisioningError("registry path must not be inside the new daemon home")

    template = None
    if template_home is not None:
        if not isinstance(template_home, str | os.PathLike):
            raise ProvisioningError("template_home must be a path")
        template = _expand_user(Path(template_home), "template_home")
        if not template.is_absolute():
            raise ProvisioningError("template_home must be an absolute path after '~' expansion")
        if not template.is_dir():
            raise ProvisioningError("template_home must be an existing directory")
        if registry_resolved == (template / "config.toml").resolve():
            raise ProvisioningError("registry path must not point at the template's config.toml")
    provider_table, catalog_path = _template_settings(template, provider)
    if catalog_text is not None:
        if not isinstance(catalog_text, str):
            raise ProvisioningError("catalog_text must be JSON text")
        try:
            catalog = json.loads(catalog_text)
        except (ValueError, TypeError):
            raise ProvisioningError("catalog_text must be a JSON model catalog") from None
        if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
            raise ProvisioningError("catalog_text must contain a models array")
        if catalog_path is not None:
            raise ProvisioningError("use either catalog_text or a template catalog reference")
        catalog_path = str(home_path / "model-catalog.json")

    existing = _load_registry(registry)["backends"]
    if name in existing:
        raise ProvisioningError(
            f"backend {name!r} already exists in the registry; refusing to overwrite"
        )
    for existing_name, item in existing.items():
        if Path(item["home"]).expanduser().resolve() == home_resolved:
            raise ProvisioningError(
                f"home is already used by backend {existing_name!r}; homes must be independent"
            )

    config: dict[str, Any] = {
        "model_provider": provider,
        "model": model_name,
    }
    if effort_value is not None:
        config["model_reasoning_effort"] = effort_value
    config["approval_policy"] = "never"
    config["sandbox_mode"] = "workspace-write"
    if catalog_path is not None:
        config["model_catalog_json"] = catalog_path
    if provider_table is not None:
        config["model_providers"] = {provider: provider_table}
    config_text = _toml_dumps(config)

    entry: dict[str, Any] = {
        "home": str(home_path),
        "model_provider": provider,
        "default_model": model_name,
    }
    if effort_value is not None:
        entry["default_effort"] = effort_value
    entry["enabled"] = False

    backend = Backend(
        name=name,
        home=home_path,
        model_provider=provider,
        default_model=model_name,
        default_effort=effort_value,
        enabled=False,
    )
    try:
        os.mkdir(home_path, 0o700)
    except FileExistsError:
        raise ProvisioningError("home already exists; refusing to overwrite") from None
    except OSError as error:
        raise ProvisioningError(f"cannot create home directory ({error.strerror})") from None
    try:
        os.chmod(home_path, 0o700)
        _write_private_config(home_path / "config.toml", config_text)
        if catalog_text is not None:
            _write_private_config(home_path / "model-catalog.json", catalog_text)
        _register(registry, name, entry)
    except OSError as error:
        _rollback_home(home_path)
        raise ProvisioningError(
            f"cannot write daemon config or registry ({error.strerror})"
        ) from None
    except BaseException:
        _rollback_home(home_path)
        raise
    return backend


def set_backend_enabled(
    name: str,
    enabled: bool,
    *,
    registry_path: Path | None = None,
) -> Backend:
    """Enable or disable one registered route; preserve every other entry.

    Uses the same flock + atomic replace as create_daemon and only touches the
    target route's 'enabled' flag; all other backends and all other fields of
    the target are preserved verbatim. The bridge performs no model probing and
    starts no daemon here: callers enable a route after their own availability
    verification. Returns the updated Backend. Setting the value it already has
    is a no-op that leaves the registry file byte-identical.
    """
    if not isinstance(name, str) or not _BACKEND_NAME_RE.fullmatch(name):
        raise ProvisioningError(
            "name must start with a lowercase ASCII letter and contain only "
            "lowercase ASCII letters, digits, '_' or '-'"
        )
    if len(name) > _MAX_TEXT_LENGTH:
        raise ProvisioningError(f"name must be at most {_MAX_TEXT_LENGTH} characters")
    if not isinstance(enabled, bool):
        raise ProvisioningError("enabled must be a boolean")
    registry, _source = _resolve_config_path(registry_path)
    if not os.path.lexists(registry):
        raise ProvisioningError(f"backend {name!r} not found; registry does not exist")
    try:
        with _registry_lock(registry):
            data = _load_registry(registry)
            existing = data["backends"]
            if name not in existing:
                raise ProvisioningError(f"backend {name!r} not found in the registry")
            entry = dict(existing[name])
            if entry.get("enabled", True) == enabled:
                return _backend_from_entry(name, entry)
            entry["enabled"] = enabled
            merged = {**existing, name: entry}
            _atomic_write(registry, _toml_dumps({**data, "backends": merged}))
            return _backend_from_entry(name, entry)
    except OSError as error:
        raise ProvisioningError(f"cannot update registry ({error.strerror})") from None


# ---------------------------------------------------------------------------
# Lifecycle routing (no shell, no restart, no credential access).
# ---------------------------------------------------------------------------


def _decoded(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _write_action_log(
    home: Path, action: str, stdout: str | bytes | None, stderr: str | bytes | None
) -> Path | None:
    """Failure logs go to a private 0600 file under the backend home, never to errors."""
    try:
        log_dir = home / "daemon-logs"
        log_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(log_dir, 0o700)
        path = log_dir / f"daemon-{action}.log"
        text = "stdout:\n" + _decoded(stdout) + "\nstderr:\n" + _decoded(stderr) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, text.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
        return path
    except OSError:
        return None


def _log_suffix(log: Path | None) -> str:
    return f"; private log: {log}" if log is not None else "; log could not be written"


def _check_start_binding(backend: Backend, home: Path) -> None:
    config_path = home / "config.toml"
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ProvisioningError(
            f"backend {backend.name!r}: home has no config.toml; create it with create_daemon"
        ) from None
    except OSError:
        raise ProvisioningError(f"backend {backend.name!r}: config.toml cannot be read") from None
    except tomllib.TOMLDecodeError:
        raise ProvisioningError(
            f"backend {backend.name!r}: config.toml is not valid TOML"
        ) from None
    if data.get("model_provider") != backend.model_provider:
        # Do not echo the file's value: it is untrusted and may be anything.
        raise ProvisioningError(
            f"backend {backend.name!r}: config.toml model_provider does not match the "
            f"backend binding {backend.model_provider!r}; refusing to start "
            "(fix the config; status/stop remain available)"
        )


def _status_result(backend: Backend, home: Path, result: subprocess.CompletedProcess) -> dict:
    socket_path = home / "app-server-control" / "app-server-control.sock"
    base = {
        "backend": backend.name,
        "home": str(home),
        "action": "status",
        "socketPath": str(socket_path),
    }
    if result.returncode != 0:
        if not socket_path.exists():
            return {**base, "status": "stopped"}
        if "failed to connect" in (result.stderr or ""):
            return {**base, "status": "unreachable"}
        log = _write_action_log(home, "status", result.stdout, result.stderr)
        raise BridgeError(
            f"cannot query daemon status for backend {backend.name!r} "
            f"(exit {result.returncode}); check the codex installation{_log_suffix(log)}"
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        log = _write_action_log(home, "status", result.stdout, result.stderr)
        raise BridgeError(
            f"daemon status for backend {backend.name!r} returned invalid JSON{_log_suffix(log)}"
        )
    merged = {**base, **payload}
    merged["backend"] = backend.name
    merged["home"] = str(home)
    merged.setdefault("socketPath", str(socket_path))
    return merged


def daemon_action(
    backend: Backend,
    action: Literal["start", "status", "stop"],
    *,
    timeout: float = 45,
) -> dict[str, Any]:
    """Run 'codex app-server daemon start|version|stop' for one backend.

    The environment is inherited with only CODEX_HOME overridden to the backend
    home; no shell is used and provider credentials are never read or written
    by the bridge. 'start' first verifies that home/config.toml binds the same
    model_provider as the backend; 'status'/'stop' skip that check so a
    misconfigured home can still be inspected and stopped. There is no restart
    and no automatic retry. The registry 'enabled' flag is not consulted here;
    gating routes is the caller's responsibility.

    Returns a structured dict. 'status' maps to the native 'version' command:
    exit 0 parses its JSON payload; a non-zero exit with no socket means
    'stopped', and with a socket plus 'failed to connect' means 'unreachable'.
    For start/stop the returned status reflects the CLI exit status only;
    confirm readiness with 'status'. Failures and timeouts raise BridgeError
    without echoing command output; the output is written to a private log
    under home/daemon-logs when possible.
    """
    if not isinstance(backend, Backend):
        raise ProvisioningError("backend must be a config.Backend instance")
    if not isinstance(action, str) or action not in _DAEMON_SUBCOMMANDS:
        raise ProvisioningError(
            "action must be 'start', 'status' or 'stop' (restart is not supported)"
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ProvisioningError("timeout must be a positive finite number of seconds")
    home = _expand_user(Path(backend.home), f"backend {backend.name!r}: home")
    if not home.is_absolute():
        raise ProvisioningError(f"backend {backend.name!r}: home must be an absolute path")
    if not home.is_dir():
        raise ProvisioningError(f"backend {backend.name!r}: home directory does not exist")
    if action == "start":
        _check_start_binding(backend, home)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    command = ["codex", "app-server", "daemon", _DAEMON_SUBCOMMANDS[action]]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        log = _write_action_log(home, action, error.stdout, error.stderr)
        raise BridgeError(
            f"daemon {action} for backend {backend.name!r} timed out after {timeout}s; "
            f"outcome unknown - query 'status' before retrying{_log_suffix(log)}"
        ) from error
    except OSError as error:
        raise BridgeError(
            f"cannot execute codex CLI for backend {backend.name!r} ({error.strerror})"
        ) from error

    if action == "status":
        return _status_result(backend, home, result)
    if result.returncode != 0:
        log = _write_action_log(home, action, result.stdout, result.stderr)
        raise BridgeError(
            f"daemon {action} for backend {backend.name!r} failed with exit code "
            f"{result.returncode}{_log_suffix(log)}"
        )
    return {
        "backend": backend.name,
        "home": str(home),
        "action": action,
        "status": "started" if action == "start" else "stopped",
        "socketPath": str(home / "app-server-control" / "app-server-control.sock"),
    }
