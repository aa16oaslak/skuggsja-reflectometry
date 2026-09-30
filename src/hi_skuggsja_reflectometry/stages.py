from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import clock
from .config import StageConfig

if TYPE_CHECKING:
    import libximc.highlevel as ximc


# libximc flag values, spelled out so that moving and limit-setting work
# without loading the native library (the simulated stages need neither).
# libximc accepts plain ints for these and reports status as int flags.
MVCMD_ERROR = 0x40  # MvcmdStatus.MVCMD_ERROR
MVCMD_RUNNING = 0x80  # MvcmdStatus.MVCMD_RUNNING
MVCMD_NAME_BITS = 0x3F  # which command the move status is about
MVCMD_MOVE, MVCMD_MOVR, MVCMD_STOP, MVCMD_HOME = 0x01, 0x02, 0x05, 0x06
_COMMAND_NAMES = {
    0x00: "none", 0x01: "move", 0x02: "relative move", 0x03: "turn left", 0x04: "turn right",
    0x05: "stop", 0x06: "home", 0x07: "loft", 0x08: "soft stop",
}
HOME_START_TIMEOUT = 2.0  # seconds for the controller to take up a home command
BORDER_IS_ENCODER = 0x01  # borders are the LeftBorder/RightBorder positions, not limit switches
BORDER_STOP_LEFT = 0x02
BORDER_STOP_RIGHT = 0x04
STATE_IS_HOMED = 0x20  # StateFlags: homed since the controller was switched on
HOME_DIR_FIRST = 0x01  # HomeFlags: homing starts towards increasing counts ("right") if set

RECEIVER, SAMPLE = "receiver", "sample"
STAGE_LABELS = {RECEIVER: "Receiver R1", SAMPLE: "Sample R2"}
HOMING_ORDER = (SAMPLE, RECEIVER)  # the sample first, as the sweeps' --set-zero always did


class HardwareUnavailable(RuntimeError):
    """Raised when the Standa stage hardware can't be reached: the libximc
    native library failed to load, or a device couldn't be opened."""


def _load_ximc():
    """Imports libximc.highlevel on demand, so merely importing this module
    (e.g. for --help or config init) never touches the native library --
    only actually opening or moving a stage does."""
    try:
        import libximc.highlevel as ximc
    except Exception as exc:
        raise HardwareUnavailable(
            "Could not load the Standa libximc native library.\n"
            "Make sure the vendor driver is installed on this machine "
            "(on Windows, also the Microsoft Visual C++ 2013 Redistributable).\n"
            f"Original error: {exc}"
        ) from exc
    return ximc


def degrees_to_microsteps(resolution: float, angle: float) -> int:
    """Returns microsteps for a given angle."""
    return int(angle / resolution)


def set_speed_accel(axis: ximc.Axis, speed: float, accel: float) -> None:
    """Set speed and acceleration for a rotation stage, checking that both
    are within the stage's hardware limits."""
    if not 0 < speed <= 10:
        raise ValueError("Speed must be between 0 and max speed.")
    if not 0 < accel <= 10:
        raise ValueError("Acceleration must be between 0 and max acceleration.")
    mvst = axis.get_move_settings()
    mvst.Speed = int(speed)
    mvst.Accel = int(accel)
    axis.set_move_settings(mvst)


def wait_for_stop(axis: ximc.Axis, stop_event: threading.Event, poll_ms: int = 100) -> None:
    """Polls the axis status while checking stop_event, instead of blocking
    on command_wait_for_stop()."""
    while not stop_event.is_set():
        status = axis.get_status()
        if not int(status.MvCmdSts) & MVCMD_RUNNING:
            break
        clock.sleep(poll_ms / 1000)

    if stop_event.is_set():
        axis.command_stop()
        raise RuntimeError("Emergency stop")


def rotate_to_angle(
    axis: ximc.Axis,
    target_angle: float,
    large: bool,
    stop_event: threading.Event,
    zero_l: float,
    zero_s: float,
    angle_min: float,
    angle_max: float,
) -> None:
    """Rotates a stage to an absolute position.

    For the large stage, the target angle is negated to get positive
    rotation, and is checked against the allowed bounds.
    """
    if large:
        zero = zero_l
        if not angle_min <= target_angle <= angle_max:
            raise ValueError("Target angle is out of bounds.")
        target_angle = -target_angle
    else:
        zero = zero_s

    axis.command_move_calb(zero + target_angle)

    wait_for_stop(axis, stop_event)

    if stop_event.is_set():
        axis.command_stop()  # ensure stage stops if ESC mid-move
        raise RuntimeError("Emergency stop during rotate_to_angle")


def rotate_relative(
    axis: ximc.Axis,
    delta_angle: float,
    large: bool,
    stop_event: threading.Event,
    zero_l: float,
    angle_min: float,
    angle_max: float,
) -> None:
    """Rotates a stage by a relative angle.

    For the large stage, the delta becomes negative and the final angle is
    checked against the allowed bounds.
    """
    if large:
        if not angle_min <= -int(axis.get_position_calb().Position) + zero_l + delta_angle <= angle_max:
            raise ValueError("Target angle is out of bounds.")
        delta_angle = -delta_angle

    axis.command_movr_calb(delta_angle)

    wait_for_stop(axis, stop_event)

    if stop_event.is_set():
        axis.command_stop()  # ensure stage stops if ESC mid-move
        raise RuntimeError("Emergency stop during rotate_relative")


def position(axis: ximc.Axis, large: bool, zero_l: float, zero_s: float) -> float:
    if large:
        return -int(axis.get_position_calb().Position) + zero_l
    else:
        return int(axis.get_position_calb().Position) - zero_s


def describe_move_status(sts: int) -> str:
    """The controller's move status (MvCmdSts) in words."""
    name = _COMMAND_NAMES.get(sts & MVCMD_NAME_BITS, f"command {sts & MVCMD_NAME_BITS:#04x}")
    parts = [f"last command {name}"]
    if sts & MVCMD_RUNNING:
        parts.append("running")
    if sts & MVCMD_ERROR:
        parts.append("ended with an error")
    return ", ".join(parts) + f" (MvCmdSts {sts:#04x})"


def describe_homing(axis: Any) -> str | None:
    """The controller's own homing settings in words, or None if they can't
    be read."""
    try:
        settings = axis.get_home_settings()
        flags = int(settings.HomeFlags)
    except Exception:  # noqa: BLE001 -- only shown to the person
        return None
    until = {0: "nothing (no stop condition set)", 1: "its revolution sensor", 2: "its sync input", 3: "a limit switch"}
    parts = [f"first towards {'increasing' if flags & 0x01 else 'decreasing'} counts until {until[(flags >> 4) & 3]}"]
    if flags & 0x04:
        parts.append(f"then towards {'increasing' if flags & 0x02 else 'decreasing'} counts until {until[(flags >> 6) & 3]}")
    delta = getattr(settings, "HomeDelta", None)
    if delta:
        parts.append(f"then {delta} steps further")
    fast = getattr(settings, "FastHome", None)
    speed = f", at {fast} steps/s" if fast is not None else ""
    return "; ".join(parts) + speed + f" (HomeFlags {flags:#05x})"


def homing_direction(axis: Any) -> int | None:
    """Which way the controller's own homing starts: +1 towards increasing
    counts, -1 towards decreasing, None if its settings can't be read."""
    try:
        flags = int(axis.get_home_settings().HomeFlags)
    except Exception:  # noqa: BLE001 -- only shown to the person, before they confirm
        return None
    return 1 if flags & HOME_DIR_FIRST else -1


def home_stage(axis: Any, stage: str, cfg: StageConfig, stop_event: threading.Event, poll_ms: int = 100) -> None:
    """Drives one stage to its home sensor with the controller's own homing
    procedure (its direction and speeds are set in the controller), and
    only when the controller reports it homed, sets its count to 0 there.

    Stopping at soft limits is switched off while a stage homes: limits are
    counted from a zero that is only right once it is homed, so before that
    they could stop it anywhere. The receiver gets its limits again, now in
    the right place, straight after; the sample's controller gets back what
    it had.

    Raises RuntimeError if a stop interrupts it or the controller does not
    report the stage homed, saying what the controller reported; the count
    is then left alone, not zeroed."""
    label = STAGE_LABELS[stage]
    if stop_event.is_set():
        raise RuntimeError(f"[{label}] emergency stop before homing")

    edges = axis.get_edges_settings()
    original_flags = int(edges.BorderFlags)
    if original_flags & (BORDER_STOP_LEFT | BORDER_STOP_RIGHT):
        edges.BorderFlags = original_flags & ~(BORDER_STOP_LEFT | BORDER_STOP_RIGHT)
        axis.set_edges_settings(edges)

    homed = False
    try:
        print(f"  [{label}] homing...")
        start = float(axis.get_position_calb().Position)
        axis.command_home()
        _wait_for_home_to_start(axis, label, stop_event, poll_ms)
        try:
            wait_for_stop(axis, stop_event, poll_ms)
        except RuntimeError:
            raise RuntimeError(f"[{label}] homing was stopped before it finished; not homed") from None
        status = axis.get_status()
        sts, flags = int(status.MvCmdSts), int(getattr(status, "Flags", 0))
        if sts & MVCMD_ERROR or not flags & STATE_IS_HOMED:
            moved = float(axis.get_position_calb().Position) - start
            raise RuntimeError(_homing_failure(label, sts, moved, axis))
        axis.command_zero()
        homed = True
    finally:
        if stage == RECEIVER and homed:
            set_boundaries(axis, cfg.res_large, cfg.angle_min, cfg.angle_max, cfg.zero_l)
        elif int(edges.BorderFlags) != original_flags:
            edges.BorderFlags = original_flags
            axis.set_edges_settings(edges)
    home_angle = cfg.zero_l if stage == RECEIVER else -cfg.zero_s
    print(f"  [{label}] homed ✓ (count 0 = {home_angle:g}° in the settings)")


def _wait_for_home_to_start(axis: Any, label: str, stop_event: threading.Event, poll_ms: int) -> None:
    """Waits until the controller's move status is about the home command,
    so a status left over from before is not taken for the end of homing."""
    deadline = clock.time() + HOME_START_TIMEOUT
    while True:
        sts = int(axis.get_status().MvCmdSts)
        if sts & MVCMD_NAME_BITS == MVCMD_HOME:
            return
        if stop_event.is_set():
            axis.command_stop()
            raise RuntimeError(f"[{label}] homing was stopped before it finished; not homed")
        if clock.time() > deadline:
            axis.command_stop()
            raise RuntimeError(
                f"[{label}] the controller did not take up the home command ({describe_move_status(sts)}); "
                "not homed, its count was left alone"
            )
        clock.sleep(poll_ms / 1000)


def _homing_failure(label: str, sts: int, moved: float, axis: Any) -> str:
    how = "it did not move at all" if abs(moved) < 0.01 else f"it moved {moved:+.2f}° in the settings' units first"
    return (
        f"[{label}] homing ended without the controller reporting the stage homed; {how}. Its count was "
        f"left alone. Controller: {describe_move_status(sts)}. Its homing settings: "
        f"{describe_homing(axis) or 'could not be read'}."
    )


def home_stages(
    large_stage: Any,
    small_stage: Any,
    stages: Sequence[str],
    cfg: StageConfig,
    on_homed: Callable[[str], None],
    stop_event: threading.Event,
) -> None:
    """Homes the given stages (RECEIVER, SAMPLE) one after the other, the
    sample first; calls on_homed(stage) after each. Ask the person to
    confirm the path is clear before calling this: nothing here asks."""
    for stage in (s for s in HOMING_ORDER if s in stages):
        home_stage(large_stage if stage == RECEIVER else small_stage, stage, cfg, stop_event)
        on_homed(stage)


def set_boundaries(axis: ximc.Axis, res: float, angle_min: float, angle_max: float, zero_l: float) -> None:
    edges = axis.get_edges_settings()
    existing_ender_flags = edges.EnderFlags
    edges.LeftBorder = degrees_to_microsteps(res, zero_l - angle_max)  # maximum angle
    edges.RightBorder = degrees_to_microsteps(res, zero_l - angle_min)  # minimum angle
    edges.BorderFlags = BORDER_IS_ENCODER | BORDER_STOP_LEFT | BORDER_STOP_RIGHT
    edges.EnderFlags = existing_ender_flags  # keep existing SW1+SW2 config
    axis.set_edges_settings(edges)


def port_name(uri: str) -> str:
    """'xi-com:\\\\.\\COM3' -> 'COM3'."""
    return uri.replace("\\", "/").rsplit("/", 1)[-1]


def serial_problem(axis: Any, large: bool, cfg: StageConfig) -> str | None:
    """Why the controller on the receiver's (large) or sample's port is not
    the one the settings pin there, or None if it is or nothing is pinned."""
    expected = cfg.serial_large if large else cfg.serial_small
    if not expected:
        return None
    try:
        actual = int(axis.get_serial_number())
    except Exception as e:  # noqa: BLE001 -- can't check it, so it doesn't pass
        return f"its controller's serial number could not be read ({e}), so it can't be checked"
    if actual == expected:
        return None
    role, other_role = (RECEIVER, SAMPLE) if large else (SAMPLE, RECEIVER)
    other = cfg.serial_small if large else cfg.serial_large
    if actual == other:
        return (
            f"it has the {other_role}'s controller ({actual}), not the {role}'s ({expected}): the two ports are "
            "swapped. Swap device_uri_large and device_uri_small in reflecto.toml"
        )
    return (
        f"it has controller {actual}, but the {role}'s is {expected} (serial_{'large' if large else 'small'} in "
        "the settings). Check which port each controller is on"
    )


def _serial_at(ximc: Any, uri: str) -> tuple[int | None, str]:
    """(serial number, None) of the controller on `uri`, read by opening it
    briefly, or (None, why it could not be read)."""
    axis = ximc.Axis(uri)
    try:
        axis.open_device()
    except Exception as e:  # noqa: BLE001 -- reported to the person
        return None, (str(e).splitlines()[0] if str(e) else type(e).__name__)
    try:
        return int(axis.get_serial_number()), ""
    except Exception as e:  # noqa: BLE001
        return None, f"opened, but its serial number could not be read ({e})"
    finally:
        try:
            axis.close_device()
        except Exception:  # noqa: BLE001, S110 -- nothing more to do with it
            pass


def locate_controllers(cfg: StageConfig) -> tuple[StageConfig, list[str]]:
    """With the controllers' serial numbers pinned in the settings, finds
    which port each is on now -- Windows numbers COM ports per PC and per
    USB socket -- and returns the settings with those ports, plus a note
    for each that differs from the ports written in the settings. Without
    pinned serials it changes nothing.

    The two ports in the settings are opened and asked for their serial
    numbers first. Only a pinned controller that is on neither is looked
    for in libximc's list of connected controllers (which comes back empty
    on some PCs). Raises HardwareUnavailable if a pinned controller is on
    no port, saying what each port had."""
    wanted = {RECEIVER: cfg.serial_large, SAMPLE: cfg.serial_small}
    if not any(wanted.values()):
        return cfg, []
    ximc = _load_ximc()
    found: dict[int, str] = {}
    seen: dict[str, str] = {}  # port -> what was on it, for the message
    for uri in dict.fromkeys((cfg.device_uri_large, cfg.device_uri_small)):
        serial, problem = _serial_at(ximc, uri)
        if serial is None:
            seen[port_name(uri)] = problem
        else:
            found.setdefault(serial, uri)
            seen[port_name(uri)] = f"controller {serial}"
    if any(serial and serial not in found for serial in wanted.values()):
        try:
            for device in ximc.enumerate_devices(ximc.EnumerateFlags.ENUMERATE_PROBE):
                serial, uri = int(device["device_serial"]), device["uri"]
                if serial not in found:
                    found[serial] = uri
                    seen.setdefault(port_name(uri), f"controller {serial}")
        except Exception:  # noqa: BLE001, S110 -- the listing is only an extra
            pass

    uris = {RECEIVER: cfg.device_uri_large, SAMPLE: cfg.device_uri_small}
    notes, missing = [], []
    for role, serial in wanted.items():
        if not serial:
            continue
        uri = found.get(serial)
        if uri is None:
            missing.append(f"the {role}'s controller ({serial})")
            continue
        if port_name(uri) != port_name(uris[role]):
            notes.append(
                f"{STAGE_LABELS[role]}: its controller ({serial}) is on {port_name(uri)}, not "
                f"{port_name(uris[role])} as written in the settings; using {port_name(uri)}."
            )
        uris[role] = uri
    if missing:
        ports = "; ".join(f"{port}: {what}" for port, what in seen.items())
        raise HardwareUnavailable(
            f"Could not find {' or '.join(missing)}. What the ports had: {ports}. Check the USB cables and "
            "power, the serial numbers in reflecto.toml, and that no other program (XILab, or reflecto in "
            "another window) has a controller open."
        )
    return replace(cfg, device_uri_large=uris[RECEIVER], device_uri_small=uris[SAMPLE]), notes


def verify_controllers(large_stage: Any, small_stage: Any, cfg: StageConfig) -> None:
    """Raises HardwareUnavailable if a port has another controller than the
    settings pin there. Call it before anything is written or moved."""
    problems = [
        f"{port_name(uri)} ({role}): {problem}."
        for axis, uri, role, large in (
            (large_stage, cfg.device_uri_large, RECEIVER, True),
            (small_stage, cfg.device_uri_small, SAMPLE, False),
        )
        if (problem := serial_problem(axis, large, cfg))
    ]
    if problems:
        raise HardwareUnavailable("Wrong controller on a port. " + " ".join(problems) + " Nothing was moved.")


def _open_axis(ximc, uri: str, label: str) -> ximc.Axis:
    axis = ximc.Axis(uri)
    try:
        axis.open_device()
    except Exception as exc:
        raise HardwareUnavailable(
            f"Could not open the {label} stage at '{uri}'.\n"
            "Check the COM port in your config, that the stage is powered "
            "and plugged in, and that no other program is using it.\n"
            f"Original error: {exc}"
        ) from exc
    return axis


def open_stages(cfg: StageConfig) -> tuple[ximc.Axis, ximc.Axis]:
    """Opens and calibrates both rotation stages, and applies the large
    stage's soft angle limits. Called explicitly from CLI commands so that
    importing this module never touches hardware."""
    ximc = _load_ximc()

    large_stage = _open_axis(ximc, cfg.device_uri_large, "large (receiver)")
    try:
        small_stage = _open_axis(ximc, cfg.device_uri_small, "small (sample)")
    except HardwareUnavailable:
        large_stage.close_device()
        raise
    try:
        verify_controllers(large_stage, small_stage, cfg)  # before any setting is written
    except HardwareUnavailable:
        for axis in (large_stage, small_stage):
            axis.close_device()
        raise
    configure_stages(large_stage, small_stage, cfg)
    return large_stage, small_stage


def configure_stages(large_stage: ximc.Axis, small_stage: ximc.Axis, cfg: StageConfig) -> None:
    """Calibrates both stages to degrees and applies the large stage's soft
    angle limits -- the same for real and simulated stages."""
    large_stage.set_calb(cfg.res_large, large_stage.get_engine_settings().MicrostepMode)
    small_stage.set_calb(cfg.res_small, small_stage.get_engine_settings().MicrostepMode)

    set_boundaries(large_stage, cfg.res_large, cfg.angle_min, cfg.angle_max, cfg.zero_l)
    print(f"Limits {cfg.angle_min}° to {cfg.angle_max}° set successfully")
