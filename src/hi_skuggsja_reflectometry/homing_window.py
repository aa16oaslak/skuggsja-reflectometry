"""The window shown while `reflecto homing` drives the stages home: the STOP
button, and which stage is homing."""
from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .stages import HOMING_ORDER, STAGE_LABELS
from .stop_window import StopWindow

if TYPE_CHECKING:
    from .emergency_stop import EmergencyStop

_REAL_BANNER, _SIM_BANNER = "#e65100", "#1565c0"


class HomingProgress:
    """Which of the stages to home are done, written by the homing thread and
    read by the window."""

    def __init__(self, stages: Sequence[str]) -> None:
        self.stages = [s for s in HOMING_ORDER if s in stages]
        self._homed: list[str] = []
        self._lock = threading.Lock()

    def homed(self, stage: str) -> None:
        with self._lock:
            self._homed.append(stage)

    def done(self) -> list[str]:
        with self._lock:
            return list(self._homed)

    def lines(self, stopped: bool) -> list[str]:
        done = self.done()
        current = next((s for s in self.stages if s not in done), None)
        lines = []
        for stage in self.stages:
            if stage in done:
                state = "homed ✓"
            elif stage == current:
                state = "stopped, not homed" if stopped else "homing..."
            else:
                state = "not homed (not started)" if stopped else "waiting"
            lines.append(f"{STAGE_LABELS[stage]}: {state}")
        return lines


class HomingWindow(StopWindow):
    poll_ms = 100

    def __init__(self, estop: EmergencyStop, title: str, progress: HomingProgress, simulated: bool) -> None:
        self.window_title = "reflecto - homing (SIMULATED)" if simulated else "reflecto - homing (real hardware)"
        super().__init__(estop, title)
        self._progress = progress
        self.hint.config(text="Esc also stops while this window is focused.\nClosing this window stops the homing.")
        tk = self.tk
        tk.Label(
            self.controls,
            text="SIMULATION - nothing moves" if simulated else "HOMING - the stages will move",
            bg=_SIM_BANNER if simulated else _REAL_BANNER, fg="white", font=("Helvetica", 12, "bold"), pady=4,
        ).pack(fill="x", before=self.status)
        self.stage_lines = tk.Label(self.controls, font=("Helvetica", 11), justify="left", anchor="w")
        self.stage_lines.pack(fill="x", padx=16, before=self.button)
        self._refresh()

    def _refresh(self) -> None:
        super()._refresh()
        self.stage_lines.config(text="\n".join(self._progress.lines(self._estop.event.is_set())))


def homing_window(progress: HomingProgress, simulated: bool):
    """A window function for run_with_emergency_stop(window=...)."""

    def show(estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
        HomingWindow(estop, title, progress, simulated).run(worker)

    return show
