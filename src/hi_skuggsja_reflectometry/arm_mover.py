"""Small moves of one arm at a time, for calibrating: turn the receiver or the
sample by a few degrees and watch where it goes on the table.

Every command is sent from a background thread, so a window using this never
waits on the hardware."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from . import clock, geometry
from .hardware import ArmReading, arm_angle
from .stages import MVCMD_ERROR, MVCMD_RUNNING, RECEIVER, SAMPLE, STAGE_LABELS

if TYPE_CHECKING:
    from .config import StageConfig

MAX_STEP = 10.0  # degrees per move; bigger turns are several clicks
MOVE_TIMEOUT = 120.0  # seconds a single small move may take


class ArmMover:
    """Moves one arm at a time by a few degrees of its angle in reflecto
    (receiver angle from Tx, sample normal from Tx), and stops them.

    The receiver's soft limits are checked before a move only once it is
    homed: before that they are counted from an unknown zero, so they are
    not where the settings say, and the controller's own copy of them is in
    the wrong place too."""

    def __init__(self, receiver_axis: Any, sample_axis: Any, cfg: StageConfig) -> None:
        self._axes = {RECEIVER: receiver_axis, SAMPLE: sample_axis}
        self._cfg = cfg
        self._lock = threading.Lock()
        self._busy: str | None = None  # the stage being moved
        self._message, self._ok = "", True
        self._sent_any = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> tuple[str | None, str, bool]:
        """(stage being moved or None, latest outcome, whether that was fine)"""
        with self._lock:
            return self._busy, self._message, self._ok

    def move(self, stage: str, delta: float, reading: ArmReading | None) -> str | None:
        """Starts turning `stage` by `delta` degrees of its angle, from
        `reading` (where it is now). Returns why it can't, or None once the
        move has been started."""
        if not 0 < abs(delta) <= MAX_STEP:
            return f"Move at most {MAX_STEP:g}° at a time; bigger turns are several clicks."
        if reading is None:
            return f"{STAGE_LABELS[stage]} can't be read, so it isn't moved."
        if reading.moving:
            return f"{STAGE_LABELS[stage]} is moving. Wait until it has stopped."
        large = stage == RECEIVER
        target = arm_angle(reading, large, self._cfg) + delta
        lo, hi = self._cfg.angle_min, self._cfg.angle_max
        if large and reading.homed and not lo <= target <= hi:
            return f"That would take the receiver to {target:.2f}°, outside its soft limits {lo:g}°-{hi:g}°."
        with self._lock:
            if self._busy:
                return "Wait until the current move has finished."
            self._busy, self._sent_any = stage, True
            self._message, self._ok = f"{STAGE_LABELS[stage]}: moving {delta:+g}°...", True
            self._stop.clear()
        # a larger receiver angle is a smaller count (angle = zero_l - count)
        count_delta = -delta if large else delta
        self._thread = threading.Thread(
            target=self._run, args=(stage, delta, count_delta, target), name="arm-mover", daemon=True
        )
        self._thread.start()
        return None

    def _run(self, stage: str, delta: float, count_delta: float, target: float) -> None:
        axis, label = self._axes[stage], STAGE_LABELS[stage]
        ok = True
        try:
            axis.command_movr_calb(count_delta)
            deadline = clock.time() + MOVE_TIMEOUT
            status = axis.get_status()
            while int(status.MvCmdSts) & MVCMD_RUNNING and not self._stop.is_set():
                if clock.time() > deadline:
                    raise TimeoutError(f"still moving after {MOVE_TIMEOUT:g} s")
                clock.sleep(0.05)
                status = axis.get_status()
            if self._stop.is_set():
                message, ok = f"{label}: stopped.", False
            elif int(status.MvCmdSts) & MVCMD_ERROR:
                message, ok = f"{label}: the controller ended the move early (at a soft limit?).", False
            else:
                message = f"{label}: moved {delta:+g}° (to {geometry.within_turn(target):.2f}°)."
        except Exception as e:  # noqa: BLE001 -- shown in the window; the stage is stopped
            message, ok = f"{label}: move failed ({type(e).__name__}: {e}). Stopped.", False
            try:
                axis.command_stop()
            except Exception:  # noqa: BLE001, S110 -- already reporting the failure
                pass
        with self._lock:
            self._busy, self._message, self._ok = None, message, ok

    def _send_stops(self) -> None:
        for axis in self._axes.values():
            try:
                axis.command_stop()
            except Exception:  # noqa: BLE001, S110 -- one failed stop must not skip the other stage
                pass

    def stop(self) -> None:
        """Stops both stages now, without waiting for the hardware."""
        self._stop.set()
        threading.Thread(target=self._send_stops, name="arm-stop", daemon=True).start()

    def close(self) -> None:
        """Before the stages are let go: stops them if anything was sent (a
        controller carries on with a move after the program lets go of it)
        and waits for the move thread to end."""
        self._stop.set()
        if self._sent_any:
            self._send_stops()
        if self._thread is not None:
            self._thread.join(timeout=5)
