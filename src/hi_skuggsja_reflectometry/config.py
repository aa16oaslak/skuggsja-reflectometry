from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_DEFAULT_CONFIG_RESOURCE = "config_default.toml"
_LOCAL_CONFIG_NAME = "reflecto.toml"
RECEIVER_TURNS = ("clockwise", "counterclockwise")


class ConfigError(ValueError):
    """A problem with a settings file, worded for the person editing it."""


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
    # Serial numbers of the controllers that belong on each port ('reflecto
    # check' shows them); 0 = not checked.
    serial_large: int = 0
    serial_small: int = 0


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
class ViewConfig:
    """How the live views draw the setup seen from above. Only the drawing:
    never changes a move."""

    # The way the receiver moves away from Tx as its angle grows.
    receiver_turns: str = "clockwise"
    # Where Tx is drawn, in degrees counterclockwise from the right:
    # 0 right (as in the paper's Fig. 1), 90 up, 180 left, 270 down.
    tx_direction: float = 0.0

    def __post_init__(self) -> None:
        if self.receiver_turns not in RECEIVER_TURNS:
            raise ConfigError(
                f'[view] receiver_turns must be "clockwise" or "counterclockwise", not {self.receiver_turns!r}.'
            )
        try:
            self.tx_direction = float(self.tx_direction) % 360
        except (TypeError, ValueError):
            raise ConfigError(f"[view] tx_direction must be a number of degrees, not {self.tx_direction!r}.") from None


@dataclass
class AppConfig:
    stages: StageConfig
    toptica: TopticaConfig
    scan_defaults: ScanDefaults
    view: ViewConfig = field(default_factory=ViewConfig)


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


def settings_path(explicit_path: Path | None = None) -> Path:
    """The local settings file in use: `explicit_path`, or ./reflecto.toml
    (which may not exist yet)."""
    return explicit_path if explicit_path is not None else Path.cwd() / _LOCAL_CONFIG_NAME


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Settings file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None


def _check_keys(override: dict[str, Any], defaults: dict[str, Any], path: Path) -> None:
    """A mistyped key would otherwise be ignored, and its default used,
    without a word -- for a calibration value, that moves the hardware
    somewhere else."""
    for section, values in override.items():
        if section not in defaults:
            known = ", ".join(f"[{name}]" for name in defaults)
            raise ConfigError(f"{path}: unknown section [{section}]. The sections are {known}.")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: {section} must be a [{section}] section heading, not a single value.")
        for key in values:
            if section == "stages" and key == "receiver_turns":
                raise ConfigError(
                    f"{path}: receiver_turns has moved from [stages] to [view]. Delete that line; "
                    "'reflecto position' shows the drawing and can save the setting under [view]."
                )
            if key not in defaults[section]:
                known = ", ".join(defaults[section])
                raise ConfigError(f"{path}: unknown setting {key} under [{section}]. The settings there are: {known}.")


def load_config(explicit_path: Path | None = None) -> AppConfig:
    """Load the packaged default config, then merge a local override on top:
    `explicit_path` if given, otherwise `./reflecto.toml` if it exists in the
    current directory. Raises ConfigError, naming the file and the setting,
    for anything it cannot use."""
    data = _read_default_toml()

    override_path = explicit_path or _find_local_config()
    if override_path is not None:
        override = _read_toml(override_path)
        _check_keys(override, data, override_path)
        data = _merge(data, override)

    try:
        return AppConfig(
            stages=StageConfig(**data["stages"]),
            toptica=TopticaConfig(**data["toptica"]),
            scan_defaults=ScanDefaults(**data["scan_defaults"]),
            view=ViewConfig(**data["view"]),
        )
    except ConfigError as e:
        raise ConfigError(f"{override_path}: {e}" if override_path else str(e)) from None


_SECTION_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?$")
_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _set_keys(text: str, section: str, values: dict[str, str]) -> str:
    """`text` with a `key = value` line for each of `values` in [section]:
    existing lines replaced in place, missing ones added at the end of the
    section, and the section added at the end if there is none."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if (m := _SECTION_HEADER.match(line)) and m.group(1) == section), None
    )
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        block = [f"[{section}]", *(f"{key} = {value}" for key, value in values.items())]
        return "\n".join([*lines, ""] + block if lines else block) + "\n"
    end = next((j for j in range(start + 1, len(lines)) if lines[j].lstrip().startswith("[")), len(lines))
    missing = dict(values)
    for j in range(start + 1, end):
        m = _KEY_LINE.match(lines[j])
        if m and m.group(1) in missing:
            lines[j] = f"{m.group(1)} = {missing.pop(m.group(1))}"
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = [f"{key} = {value}" for key, value in missing.items()]
    return "\n".join(lines) + "\n"


def save_view(path: Path, view: ViewConfig) -> None:
    """Writes `view` into the [view] section of the settings file at `path`
    (adding the section, or the file, if needed) and changes nothing else in
    it. The result is checked before anything is written; if it would come
    out wrong, ConfigError is raised and the file is left as it was."""
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        before = tomllib.loads(old)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML, so it was not changed: {e}") from None
    new = _set_keys(old, "view", {"receiver_turns": f'"{view.receiver_turns}"', "tx_direction": f"{view.tx_direction:g}"})
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        after = None
    expected_view = {**before.get("view", {}), "receiver_turns": view.receiver_turns, "tx_direction": view.tx_direction}
    if (
        after is None
        or after.get("view") != expected_view
        or {k: v for k, v in after.items() if k != "view"} != {k: v for k, v in before.items() if k != "view"}
    ):
        raise ConfigError(f"Could not update [view] in {path} safely, so it was not changed.")
    path.write_text(new, encoding="utf-8")


def write_default_config(dest: Path) -> None:
    """Copy the packaged default config to `dest` for the user to edit."""
    dest.write_text(_default_config_text(), encoding="utf-8")
