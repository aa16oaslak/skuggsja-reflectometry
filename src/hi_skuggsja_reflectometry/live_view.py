"""Live top-down view of the setup during a simulated sweep, drawn like
Fig. 1 of Singh et al., arXiv:2407.05512, next to the STOP button.

Angles are drawn clockwise from the transmitter (Tx, fixed, to the right),
as in the paper's figure. The receiver (Rx) moves on the R1 ring; the
sample turns on R2 in the middle."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import clock, geometry
from .stop_window import StopWindow

if TYPE_CHECKING:
    import threading

    from .config import StageConfig
    from .emergency_stop import EmergencyStop
    from .simulation import SimRig
    from .sweeps import PlannedStep

_BEAM, _RING, _RANGE = "#757575", "#9e9e9e", "#424242"
_SAMPLE, _NORMAL, _GHOST = "#6d4c41", "#8d6e63", "#bdbdbd"
_RX, _RX_MOVING, _BAD = "#37474f", "#1565c0", "#c62828"
_DONE, _CURRENT = "#2e7d32", "#ef6c00"


class LiveSweepWindow(StopWindow):
    """The STOP window plus a live drawing of the simulated setup and a
    panel of readouts: stage angles against the plan, phi1/phi2, and scan
    progress."""

    poll_ms = 50
    window_title = "reflecto - SIMULATION (no hardware)"
    SIZE = 540  # canvas, px
    RING = 205  # R1 ring radius, px

    def __init__(
        self,
        estop: EmergencyStop,
        title: str,
        rig: SimRig,
        plan: list[PlannedStep],
        files: list[str],
        freq_points: int,
        stage_cfg: StageConfig,
    ) -> None:
        super().__init__(estop, title)
        tk = self.tk
        from tkinter import ttk

        self._rig, self._plan, self._files = rig, plan, files
        self._limits = (stage_cfg.angle_min, stage_cfg.angle_max)
        self._receiver_home = geometry.receiver_angle(0.0, stage_cfg.zero_l)

        tk.Label(
            self.controls, text="SIMULATION - no hardware is used",
            bg=_RX_MOVING, fg="white", font=("Helvetica", 12, "bold"), pady=4,
        ).pack(fill="x", before=self.status)
        self.status.config(wraplength=340)
        self.details = tk.Label(self.controls, font=("Courier", 10), justify="left", anchor="w")
        self.details.pack(fill="x", padx=16, pady=(4, 0), before=self.button)
        self.progress = ttk.Progressbar(self.controls, maximum=max(freq_points, 1), length=340)
        self.progress.pack(padx=16, pady=(4, 2), before=self.button)
        self.warning = tk.Label(
            self.controls, fg=_BAD, font=("Helvetica", 10, "bold"), wraplength=340, justify="left"
        )
        self.warning.pack(fill="x", padx=16, before=self.button)

        self.canvas = tk.Canvas(self.root, width=self.SIZE, height=self.SIZE, bg="white", highlightthickness=0)
        self.canvas.pack(side="left")
        self._refresh()

    # -- state -------------------------------------------------------------------

    def _step_state(self) -> tuple[int, bool]:
        """(index of the step being scanned or moved to, scanning now?)"""
        started = len(self._rig.toptica.scans)
        scanning = self._rig.toptica.current is not None
        return (started - 1 if scanning else started), scanning

    def _refresh(self) -> None:
        super()._refresh()
        if not self._estop.event.is_set():
            self.status.config(text=self._title)  # time is shown in the panel, as setup time
        sample, receiver = self._rig.angles()
        k, scanning = self._step_state()
        self._draw(sample, receiver, k, scanning)
        self._update_panel(sample, receiver, k, scanning)

    def _update_panel(self, sample: float, receiver: float, k: int, scanning: bool) -> None:
        rig, n = self._rig, len(self._plan)
        phi1, phi2 = geometry.phi_angles(sample, receiver)
        if scanning:
            activity = "scanning"
        elif rig.large.moving or rig.small.moving:
            which = [name for name, axis in (("receiver", rig.large), ("sample", rig.small)) if axis.moving]
            activity = "moving " + " & ".join(which)
        else:
            activity = "done" if k >= n else "waiting"
        planned = self._plan[k] if k < n else None
        plan_s = f"plan {planned.sample:7.2f}°" if planned else ""
        plan_r = f"plan {planned.receiver:7.2f}°" if planned else ""
        scan = rig.toptica.current
        freq = f"{scan.freq:8.2f} GHz  {scan.points}/{int(self.progress['maximum'])}" if scan and scan.freq else "-"
        lines = [
            f"Step         {min(k + 1, n)} of {n}  ({activity})",
            f"Sample R2    {sample:7.2f}°  {plan_s}",
            f"Receiver R1  {receiver:7.2f}°  {plan_r}",
            f"φ1 incidence {phi1:7.2f}°",
            f"φ2 receiver  {phi2:7.2f}°  {'specular' if geometry.is_specular(sample, receiver) else ''}",
            f"Frequency    {freq}",
            (
                f"Setup time   {clock.hms(clock.time() - rig.t_start)}  (x{clock.speed():g}, "
                f"real {clock.hms(time.monotonic() - self._t0)})"
            ),
            f"File  {Path(self._files[k]).name if k < n else '-'}",
        ]
        self.details.config(text="\n".join(lines))
        self.progress["value"] = scan.points if scan else (self.progress["maximum"] if k >= n else 0)

        warnings = []
        lo, hi = self._limits
        if not lo - 0.01 <= receiver <= hi + 0.01:
            warnings.append(f"Receiver at {receiver:.2f}°, outside the soft limits {lo:g}°-{hi:g}°.")
        if scanning and planned and (abs(sample - planned.sample) > 0.01 or abs(receiver - planned.receiver) > 0.01):
            warnings.append("Scanning at different angles than planned for this step.")
        if any(not lo <= step.receiver <= hi for step in self._plan):
            warnings.append("Some planned receiver angles are outside the soft limits (red on the ring).")
        self.warning.config(text="\n".join(warnings))

    # -- drawing -------------------------------------------------------------------

    def _xy(self, angle: float, radius: float) -> tuple[float, float]:
        a = math.radians(angle)
        c = self.SIZE / 2
        return c + radius * math.cos(a), c + radius * math.sin(a)

    def _box(self, angle: float, radius: float, half: float, **style) -> None:
        """A square on the ring, turned to face the centre."""
        x, y = self._xy(angle, radius)
        a = math.radians(angle)
        ux, uy, vx, vy = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        points = [coord for sx, sy in corners for coord in (x + (sx * ux + sy * vx) * half, y + (sx * uy + sy * vy) * half)]
        self.canvas.create_polygon(*points, **style)

    def _arc(self, radius: float, start: float, end: float, **style) -> None:
        c = self.SIZE / 2
        # Tk measures arcs counter-clockwise; our angles run clockwise.
        self.canvas.create_arc(
            c - radius, c - radius, c + radius, c + radius,
            start=-end, extent=end - start, style="arc", **style,
        )

    def _draw(self, sample: float, receiver: float, k: int, scanning: bool) -> None:
        cv, R, c = self.canvas, self.RING, self.SIZE / 2
        cv.delete("all")
        lo, hi = self._limits

        # R1 ring: dotted, solid over the receiver's allowed range
        cv.create_oval(c - R, c - R, c + R, c + R, outline=_RING, dash=(2, 4), width=2)
        self._arc(R, lo, hi, outline=_RANGE, width=5)
        hx, hy = self._xy(self._receiver_home, R + 16)
        cv.create_text(hx, hy, text="home", fill=_RING, font=("Helvetica", 8))

        # planned receiver positions
        for i, step in enumerate(self._plan):
            x, y = self._xy(step.receiver, R + 14)
            outside = not lo <= step.receiver <= hi
            if i < k:
                fill, outline = _DONE, _DONE
            elif i == k:
                fill, outline = _CURRENT, _CURRENT
            else:
                fill, outline = "white", _BEAM
            if outside:
                outline = _BAD
            cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill=fill, outline=outline, width=2)
            if len(self._plan) <= 12:
                tx, ty = self._xy(step.receiver, R + 28)
                cv.create_text(tx, ty, text=str(i + 1), fill=_BEAM, font=("Helvetica", 8))

        # where a specular receiver would be, if we are not there
        if not geometry.is_specular(sample, receiver):
            x, y = self._xy(2 * sample, R)
            cv.create_line(c, c, x, y, fill=_GHOST, dash=(2, 3))
            self._box(2 * sample, R, 10, fill="", outline=_GHOST, width=2)
            gx, gy = self._xy(2 * sample, R - 28)
            cv.create_text(gx, gy, text="specular", fill=_GHOST, font=("Helvetica", 8))

        # beams: Tx -> sample -> Rx
        cv.create_line(c, c, *self._xy(0, R), fill=_BEAM, width=2)
        cv.create_line(c, c, *self._xy(receiver, R), fill=_BEAM, width=2)

        # sample normal and phi angles
        cv.create_line(c, c, *self._xy(sample, R - 12), fill=_NORMAL, dash=(6, 4))
        self._arc(55, 0, sample, outline=_SAMPLE)
        self._arc(80, sample, receiver, outline=_SAMPLE)
        phi1, phi2 = geometry.phi_angles(sample, receiver)
        x, y = self._xy(sample / 2, 70)
        cv.create_text(x, y, text=f"φ1 {phi1:.1f}°", fill=_SAMPLE, font=("Helvetica", 9), anchor="w")
        x, y = self._xy((sample + receiver) / 2, 95)
        cv.create_text(x, y, text=f"φ2 {phi2:.1f}°", fill=_SAMPLE, font=("Helvetica", 9), anchor="w")

        # sample on R2, perpendicular to its normal
        (x0, y0), (x1, y1) = self._xy(sample + 90, 42), self._xy(sample - 90, 42)
        cv.create_line(x0, y0, x1, y1, fill=_SAMPLE, width=6)
        cv.create_oval(c - 4, c - 4, c + 4, c + 4, fill="white", outline=_SAMPLE, width=2)
        x, y = self._xy(sample - 90, 58)
        cv.create_text(x, y, text="Sample (R2)", fill=_SAMPLE, font=("Helvetica", 9, "bold"))

        # Tx (fixed) and Rx on R1
        self._box(0, R, 12, fill="white", outline=_RX, width=2)
        x, y = self._xy(0, R)
        cv.create_text(x, y - 24, text="Tx", fill=_RX, font=("Helvetica", 11, "bold"))
        moving = self._rig.large.moving
        outside = not lo - 0.01 <= receiver <= hi + 0.01
        colour = _BAD if outside else (_RX_MOVING if moving else _RX)
        self._box(receiver, R, 12, fill=colour, outline=colour)
        x, y = self._xy(receiver, R - 30)
        cv.create_text(x, y, text="Rx", fill=colour, font=("Helvetica", 11, "bold"))

        # legend
        cv.create_line(12, 16, 40, 16, fill=_RANGE, width=5)
        cv.create_text(46, 16, text=f"receiver range (soft limits {lo:g}°-{hi:g}°)", anchor="w",
                       fill=_RANGE, font=("Helvetica", 9))
        for j, (fill, label) in enumerate(((_DONE, "scanned"), (_CURRENT, "current"), ("white", "planned"))):
            y = 36 + 16 * j
            cv.create_oval(21, y - 5, 31, y + 5, fill=fill, outline=_BEAM if fill == "white" else fill, width=2)
            cv.create_text(46, y, text=label, anchor="w", fill=_RANGE, font=("Helvetica", 9))
        cv.create_text(self.SIZE - 8, self.SIZE - 10, anchor="e", fill=_GHOST, font=("Helvetica", 8),
                       text="Top view after Fig. 1 of Singh et al., arXiv:2407.05512")


def live_window(rig: SimRig, plan: list[PlannedStep], files: list[str], freq_points: int, stage_cfg: StageConfig):
    """A window function for run_with_emergency_stop(window=...)."""

    def show(estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
        LiveSweepWindow(estop, title, rig, plan, files, freq_points, stage_cfg).run(worker)

    return show
