from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from .config import StageConfig

if TYPE_CHECKING:
    import libximc.highlevel as ximc


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
    MvcmdStatus = _load_ximc().MvcmdStatus
    while not stop_event.is_set():
        status = axis.get_status()
        if not (MvcmdStatus.MVCMD_RUNNING in status.MvCmdSts):
            break
        time.sleep(poll_ms / 1000)

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


def confirm_path_clear(min_deg: float, max_deg: float, home_large: float, home_small: float) -> bool:
    """Blocks until the user explicitly confirms the path is clear.

    Returns True only on exact match to the confirmation phrase -- this
    avoids accidental Enter-presses confirming a dangerous move.
    """
    print("\n" + "=" * 60)
    print("⚠️WARNING")
    print("=" * 60)
    print(f"To reset zero position the stages must return to home ({home_large}° and {home_small}°),")
    print(f"OUTSIDE operating boundaries ({min_deg}° to {max_deg}°).")
    print("This range is not guaranteed to be clear of obstacles.")
    print()
    print("Before continuing:")
    print("  1. Check the full rotation path is clear of cables,")
    print("     mounts, or anything that could collide with the stage.")
    print("  2. Make sure no measurement equipment is in the way.")
    print("=" * 60)

    try:
        response = input(
            "\nType 'clear' to confirm the path is clear and proceed, "
            "or anything else to cancel: "
        ).strip().lower()
    except EOFError:  # stdin closed, or Ctrl+C interrupted the read
        response = ""

    if response == "clear":
        print("✅ Confirmed — proceeding with homing.\n")
        return True
    else:
        print("❌ Homing cancelled.\n")
        return False


def home_and_zero(
    axis1: ximc.Axis,
    axis2: ximc.Axis,
    name1: str,
    name2: str,
    min_deg: float,
    max_deg: float,
    res: float,
    zero_l: float,
    zero_s: float,
    stop_event: threading.Event,
) -> None:
    """Moves both stages to home and sets zero there.

    Requires explicit user confirmation since the home position may be
    outside the normal safe bounds. A stop between or during the homing
    moves aborts the rest of the procedure.
    """
    if stop_event.is_set():
        raise RuntimeError("Emergency stop before homing")

    print(f"  [{name2}] Moving to home position ({zero_s}°)...")

    axis2.command_homezero()

    if stop_event.is_set():
        raise RuntimeError(f"[{name2}] Emergency stop during homing -- its zero may be wrong, re-run --set-zero")

    print(f"  [{name2}] Homed and zeroed ✓")

    if not confirm_path_clear(min_deg, max_deg, zero_l, -zero_s):
        raise RuntimeError(f"[{name1}] Homing aborted by user — coast not confirmed clear")

    if stop_event.is_set():
        raise RuntimeError(f"[{name1}] Emergency stop before homing")

    print(f"  [{name1}] Moving to home position ({-zero_l}°)...")

    # Temporarily widen soft limits to allow reaching home, if home is outside them
    edges = axis1.get_edges_settings()
    original_left, original_right = edges.LeftBorder, edges.RightBorder

    home_steps = degrees_to_microsteps(res, -zero_l)
    if home_steps < original_left or home_steps > original_right:
        # widen just enough to fit home position, with a little margin
        margin = degrees_to_microsteps(res, 5.0)
        edges.LeftBorder = min(original_left, home_steps - margin)
        edges.RightBorder = max(original_right, home_steps + margin)
        axis1.set_edges_settings(edges)
        print(f"  [{name1}] Temporarily widened limits to reach home")

    try:
        axis1.command_homezero()
        if stop_event.is_set():
            raise RuntimeError(f"[{name1}] Emergency stop during homing -- its zero may be wrong, re-run --set-zero")
        print(f"  [{name1}] Homed and zeroed ✓")
    finally:
        # Always restore original limits, even if move failed
        edges.LeftBorder = original_left
        edges.RightBorder = original_right
        axis1.set_edges_settings(edges)
        print(f"  [{name1}] Restored normal safe limits")


def set_boundaries(axis: ximc.Axis, res: float, angle_min: float, angle_max: float, zero_l: float) -> None:
    ximc = _load_ximc()
    edges = axis.get_edges_settings()
    existing_ender_flags = edges.EnderFlags
    edges.LeftBorder = degrees_to_microsteps(res, zero_l - angle_max)  # maximum angle
    edges.RightBorder = degrees_to_microsteps(res, zero_l - angle_min)  # minimum angle
    edges.BorderFlags = ximc.BorderFlags(0x07)  # BORDER_IS_ALIVE | BORDER_STOP_LEFT | BORDER_STOP_RIGHT
    edges.EnderFlags = existing_ender_flags  # keep existing SW1+SW2 config
    axis.set_edges_settings(edges)


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
    small_stage = _open_axis(ximc, cfg.device_uri_small, "small (sample)")

    large_stage.set_calb(cfg.res_large, large_stage.get_engine_settings().MicrostepMode)
    small_stage.set_calb(cfg.res_small, small_stage.get_engine_settings().MicrostepMode)

    set_boundaries(large_stage, cfg.res_large, cfg.angle_min, cfg.angle_max, cfg.zero_l)
    print(f"Limits {cfg.angle_min}° to {cfg.angle_max}° set successfully")

    return large_stage, small_stage
