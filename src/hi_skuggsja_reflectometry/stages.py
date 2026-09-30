from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
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

    For the receiver, stopping at the soft limits is switched off while it
    homes: the limits are counted from a zero that is only right once it is
    homed. They are set again, now in the right place, straight after.

    Raises RuntimeError if a stop interrupts it or the controller does not
    report the stage homed; the count is then left alone, not zeroed."""
    label = STAGE_LABELS[stage]
    if stop_event.is_set():
        raise RuntimeError(f"[{label}] emergency stop before homing")

    edges, original_flags = None, None
    if stage == RECEIVER:
        edges = axis.get_edges_settings()
        original_flags = int(edges.BorderFlags)
        edges.BorderFlags = original_flags & ~(BORDER_STOP_LEFT | BORDER_STOP_RIGHT)
        axis.set_edges_settings(edges)

    homed = False
    try:
        print(f"  [{label}] homing...")
        axis.command_home()
        try:
            wait_for_stop(axis, stop_event, poll_ms)
        except RuntimeError:
            raise RuntimeError(f"[{label}] homing was stopped before it finished; not homed") from None
        status = axis.get_status()
        if int(status.MvCmdSts) & MVCMD_ERROR or not int(getattr(status, "Flags", 0)) & STATE_IS_HOMED:
            raise RuntimeError(f"[{label}] the controller did not report the stage homed; its count was left alone")
        axis.command_zero()
        homed = True
    finally:
        if edges is not None:
            if homed:
                set_boundaries(axis, cfg.res_large, cfg.angle_min, cfg.angle_max, cfg.zero_l)
            else:
                edges.BorderFlags = original_flags
                axis.set_edges_settings(edges)
    home_angle = cfg.zero_l if stage == RECEIVER else -cfg.zero_s
    print(f"  [{label}] homed ✓ (count 0 = {home_angle:g}° in the settings)")


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
