from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_DEFAULT_CONFIG_RESOURCE = "config_default.toml"
_LOCAL_CONFIG_NAME = "reflecto.toml"


@dataclass
class StageConfig:
    device_uri_large: str
    device_uri_small: str
    zero_l: float
    zero_s: float
    res_large: float
    res_small: float
    angle_min: float
    angle_max: float


@dataclass
class TopticaConfig:
    host: str
    port: int
    freq_step: float
    settle_freq_tol: float
    settle_timeout: float
    amp_tol: float
    offset_tol: float
    gain_default: float
    stabilize_tol: float
    stabilize_hold: float
    stabilize_timeout: float


@dataclass
class ScanDefaults:
    freq_start: float
    freq_stop: float
    int_time: float


@dataclass
class AppConfig:
    stages: StageConfig
    toptica: TopticaConfig
    scan_defaults: ScanDefaults


def _default_config_text() -> str:
    return (
        resources.files("hi_skuggsja_reflectometry")
        .joinpath(_DEFAULT_CONFIG_RESOURCE)
        .read_text(encoding="utf-8")
    )


def _read_default_toml() -> dict[str, Any]:
    with resources.files("hi_skuggsja_reflectometry").joinpath(_DEFAULT_CONFIG_RESOURCE).open("rb") as f:
        return tomllib.load(f)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge per top-level table, so a local file only needs to set
    the keys it wants to change."""
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in override.items():
        merged.setdefault(section, {}).update(values)
    return merged


def _find_local_config() -> Path | None:
    candidate = Path.cwd() / _LOCAL_CONFIG_NAME
    return candidate if candidate.is_file() else None


def load_config(explicit_path: Path | None = None) -> AppConfig:
    """Load the packaged default config, then merge a local override on top:
    `explicit_path` if given, otherwise `./rannis_reflectometry.toml` if it
    exists in the current directory."""
    data = _read_default_toml()

    override_path = explicit_path or _find_local_config()
    if override_path is not None:
        with open(override_path, "rb") as f:
            data = _merge(data, tomllib.load(f))

    return AppConfig(
        stages=StageConfig(**data["stages"]),
        toptica=TopticaConfig(**data["toptica"]),
        scan_defaults=ScanDefaults(**data["scan_defaults"]),
    )


def write_default_config(dest: Path) -> None:
    """Copy the packaged default config to `dest` for the user to edit."""
    dest.write_text(_default_config_text(), encoding="utf-8")
