"""Load the bridge's own TOML configuration using only the standard library.

Only this bridge configuration file is read. Backend homes are validated as
existing directories, but their configuration files and credentials are never
opened. Control sockets are derived from ``home`` and cannot be configured.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Backend", "ConfigError", "load_backends"]

CONFIG_ENV_VAR = "CODEXD_BRIDGE_CONFIG"
DEFAULT_CONFIG_PATH = "~/.config/codexd-bridge/config.toml"

_BACKEND_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")
_MAX_TEXT_LENGTH = 128
_BACKEND_FIELDS = frozenset(
    {"home", "model_provider", "default_model", "default_effort", "enabled"}
)
_REQUIRED_FIELDS = ("home", "model_provider")


class ConfigError(ValueError):
    """Invalid bridge configuration.

    Messages name the offending backend and field but never echo field values
    or configuration fragments, so a misplaced secret cannot leak through
    error reporting.
    """


@dataclass(frozen=True)
class Backend:
    """One validated ``[backends.<name>]`` entry of the bridge configuration."""

    name: str
    home: Path
    model_provider: str
    default_model: str | None = None
    default_effort: str | None = None
    enabled: bool = True

    @property
    def socket_path(self) -> Path:
        """Control socket, fixed derivation from ``home``; not configurable."""
        return self.home / "app-server-control" / "app-server-control.sock"


def _expand_user(path: Path, context: str) -> Path:
    try:
        return path.expanduser()
    except RuntimeError:
        raise ConfigError(f"{context}: cannot expand '~' (unknown user)") from None


def _resolve_config_path(path: Path | None) -> tuple[Path, str]:
    if path is not None:
        source = "explicit path argument"
        raw = Path(path)
    else:
        env_value = os.environ.get(CONFIG_ENV_VAR, "")
        if env_value:
            source = f"environment variable {CONFIG_ENV_VAR}"
            raw = Path(env_value)
        else:
            source = f"default path {DEFAULT_CONFIG_PATH}"
            raw = Path(DEFAULT_CONFIG_PATH)
    return _expand_user(raw, f"config path ({source})"), source


def _read_config(config_path: Path, source: str) -> dict:
    try:
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file does not exist ({source})") from None
    except OSError:
        raise ConfigError(f"config file cannot be read ({source})") from None
    except tomllib.TOMLDecodeError:
        # Suppress the decoder message: it may quote configuration fragments.
        raise ConfigError(f"config file is not valid TOML ({source})") from None


def _text_field(backend: str, field: str, value: object, *, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"backend {backend!r}: field {field!r} must be a string")
    if not value.strip():
        raise ConfigError(f"backend {backend!r}: field {field!r} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ConfigError(
            f"backend {backend!r}: field {field!r} must be at most {max_length} characters"
        )
    return value


def _home_field(backend: str, value: object) -> Path:
    raw = _text_field(backend, "home", value)
    home = _expand_user(Path(raw), f"backend {backend!r}: field 'home'")
    if not home.is_absolute():
        raise ConfigError(
            f"backend {backend!r}: field 'home' must be an absolute path after '~' expansion"
        )
    if not home.is_dir():
        raise ConfigError(f"backend {backend!r}: field 'home' must be an existing directory")
    return home


def _parse_backend(name: str, entry: object) -> Backend:
    if not _BACKEND_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"backend {name!r}: invalid name; must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, '_' or '-'"
        )
    if not isinstance(entry, dict):
        raise ConfigError(f"backend {name!r} must be a table")
    unknown = sorted(set(entry) - _BACKEND_FIELDS)
    if unknown:
        listed = ", ".join(repr(field) for field in unknown)
        raise ConfigError(f"backend {name!r}: unknown field(s) {listed}")
    for field in _REQUIRED_FIELDS:
        if field not in entry:
            raise ConfigError(f"backend {name!r}: missing required field {field!r}")
    home = _home_field(name, entry["home"])
    provider = _text_field(
        name, "model_provider", entry["model_provider"], max_length=_MAX_TEXT_LENGTH
    )
    default_model = None
    if "default_model" in entry:
        default_model = _text_field(
            name, "default_model", entry["default_model"], max_length=_MAX_TEXT_LENGTH
        )
    default_effort = None
    if "default_effort" in entry:
        # No fixed enum here: effort is validated against the target model's
        # capabilities by the daemon-facing layer, not by config loading.
        default_effort = _text_field(
            name, "default_effort", entry["default_effort"], max_length=_MAX_TEXT_LENGTH
        )
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"backend {name!r}: field 'enabled' must be a boolean")
    return Backend(
        name=name,
        home=home,
        model_provider=provider,
        default_model=default_model,
        default_effort=default_effort,
        enabled=enabled,
    )


def load_backends(path: Path | None = None) -> dict[str, Backend]:
    """Load and validate backend definitions from the bridge configuration.

    Resolution order: explicit ``path`` argument, then ``CODEXD_BRIDGE_CONFIG``,
    then ``~/.config/codexd-bridge/config.toml``. A resolved path that does not
    exist is an error; there is no silent fallback. An empty environment
    variable value is treated as unset.
    """
    config_path, source = _resolve_config_path(path)
    data = _read_config(config_path, source)
    unknown = sorted(set(data) - {"backends"})
    if unknown:
        listed = ", ".join(repr(key) for key in unknown)
        raise ConfigError(f"unknown top-level field(s) {listed}; only 'backends' is supported")
    if "backends" not in data:
        raise ConfigError("missing required top-level field 'backends'")
    backends = data["backends"]
    if not isinstance(backends, dict):
        raise ConfigError("top-level field 'backends' must be a table of backend definitions")
    if not backends:
        raise ConfigError("top-level field 'backends' must define at least one backend")
    return {name: _parse_backend(name, entry) for name, entry in backends.items()}
