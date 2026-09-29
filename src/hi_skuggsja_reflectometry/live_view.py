"""Live top-down view of the setup during a sweep, real or simulated, drawn
like Fig. 1 of Singh et al., arXiv:2407.05512, next to the STOP button and
the hardware status lights.

Angles are drawn clockwise from the transmitter (Tx, fixed, to the right),
as in the paper's figure. The receiver (Rx) moves on the R1 ring; the
sample turns on R2 in the middle.

Everything shown comes from the sweep's own communication with the
hardware (see SweepSource); the window never talks to the hardware."""
from __future__ import annotations

import math
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import clock, geometry, toptica
from .hardware import FAIL, OK, WARN, DeviceStatus
from .hardware_window import HardwarePanel
from .stop_window import StopWindow

if TYPE_CHECKING:
    import threading

    from .config import StageConfig
    from .emergency_stop import EmergencyStop
    from .hardware import WatchedAxis
    from .monitoring import ScanSnapshot
    from .sweeps import PlannedStep

_BEAM, _RING, _RANGE = "#757575", "#9e9e9e", "#424242"
_SAMPLE, _NORMAL, _GHOST = "#6d4c41", "#8d6e63", "#bdbdbd"
_RX, _RX_MOVING, _BAD = "#37474f", "#1565c0", "#c62828"
_DONE, _CURRENT = "#2e7d32", "#ef6c00"
_SIM_BANNER, _REAL_BANNER = "#1565c0", "#e65100"


class SweepSource:
    """What the live view shows, taken from the sweep's own communication:
    stage positions from the status and position replies the sweep gets
    (via WatchedAxis), scan progress from toptica.progress, and how each
    device last answered. `checked` is the hardware check from before the
    sweep, shown until a device has been talked to. The same for real and
    simulated sweeps."""

    def __init__(
        self,
        large: WatchedAxis,
        small: WatchedAxis,
        stage_cfg: StageConfig,
        checked: list[DeviceStatus],
        simulated: bool,
    ) -> None:
        self.large, self.small = large, small
        self.cfg = stage_cfg
        self.checked = checked  # receiver, sample, TOptica
        self.simulated = simulated
        self.t_start = clock.time()

    def angles(self) -> tuple[float | None, float | None]:
        """(sample, receiver), or None for a stage whose position is not
        known yet."""
        p_large, p_small = self.large.snapshot()[0], self.small.snapshot()[0]
        sample = None if p_small is None else geometry.sample_angle(p_small, self.cfg.zero_s)
        receiver = None if p_large is None else geometry.receiver_angle(p_large, self.cfg.zero_l)
        return sample, receiver

    def scan(self) -> ScanSnapshot:
        return toptica.progress.snapshot()

    def devices(self) -> list[DeviceStatus]:
        return [
            self._stage(self.large, self.checked[0]),
            self._stage(self.small, self.checked[1]),
            self._toptica(self.checked[2]),
        ]

    def _stage(self, axis: WatchedAxis, before: DeviceStatus) -> DeviceStatus:
        last_ok, error = axis.link.snapshot()
        _, moving, error_flag, homing = axis.snapshot()
        if error:
            return replace(before, state=FAIL, summary=f"stopped answering: {error}")
        if last_ok is None:
            return before
        summary = "homing" if homing else "moving" if moving else f"idle, answered {_ago(last_ok)} ago"
        if error_flag:
            return replace(before, state=WARN, summary=f"{summary}; the last move was flagged as failed (a soft limit?)")
        return replace(before, state=OK, summary=summary)

    def _toptica(self, before: DeviceStatus) -> DeviceStatus:
        last_ok, error = toptica.link.snapshot()
        scan = self.scan()
        if error:
            return replace(before, state=FAIL, summary=f"stopped answering: {error}")
        if last_ok is None:
            return before
        if scan.active:
            at = f", {scan.freq:.2f} GHz" if scan.freq is not None else ""
            return replace(before, state=OK, summary=f"scanning{at}")
        return replace(before, state=OK, summary=f"idle between scans, answered {_ago(last_ok)} ago")


class LiveSweepWindow(StopWindow):
    """The STOP window plus the hardware lights, a live drawing of the setup,
    and readouts: stage angles against the plan, phi1/phi2, scan progress."""

    poll_ms = 50
    SIZE = 540  # canvas, px
    RING = 205  # R1 ring radius, px

    def __init__(
        self,
        estop: EmergencyStop,
        title: str,
        source: SweepSource,
        plan: list[PlannedStep],
        files: list[str],
        stage_cfg: StageConfig,
    ) -> None:
        self.window_title = (
            "reflecto - SIMULATION (no hardware)" if source.simulated else "reflecto - sweep (real hardware)"
        )
        super().__init__(estop, title)
        tk = self.tk
        from tkinter import ttk

        self._source, self._plan, self._files = source, plan, files
        self._limits = (stage_cfg.angle_min, stage_cfg.angle_max)
        self._receiver_home = geometry.receiver_angle(0.0, stage_cfg.zero_l)

        banner = (
            ("SIMULATION - no hardware is used", _SIM_BANNER)
            if source.simulated
            else ("REAL HARDWARE - the stages will move", _REAL_BANNER)
        )
        tk.Label(
            self.controls, text=banner[0], bg=banner[1], fg="white", font=("Helvetica", 12, "bold"), pady=4,
        ).pack(fill="x", before=self.status)
        self.status.config(wraplength=340)
        self.hardware = HardwarePanel(tk, self.controls, details=False, width=310)
        self.hardware.frame.pack(fill="x", padx=16, pady=(0, 6), before=self.button)
        self.details = tk.Label(self.controls, font=("Courier", 10), justify="left", anchor="w")
        self.details.pack(fill="x", padx=16, pady=(4, 0), before=self.button)
        self.progress = ttk.Progressbar(self.controls, maximum=1, length=340)
        self.progress.pack(padx=16, pady=(4, 2), before=self.button)
        self.warning = tk.Label(
            self.controls, fg=_BAD, font=("Helvetica", 10, "bold"), wraplength=340, justify="left"
        )
        self.warning.pack(fill="x", padx=16, before=self.button)

        self.canvas = tk.Canvas(self.root, width=self.SIZE, height=self.SIZE, bg="white", highlightthickness=0)
        self.canvas.pack(side="left")
        self._refresh()

    # -- state -------------------------------------------------------------------

    def _refresh(self) -> None:
        super()._refresh()
        if not self._estop.event.is_set():
            self.status.config(text=self._title)  # elapsed time is in the readouts
        sample, receiver = self._source.angles()
        scan = self._source.scan()
        k = scan.started - 1 if scan.active else scan.started  # step being scanned, or moved to
        self.hardware.show(self._source.devices())
        self._draw(sample, receiver, k, scan.active)
        self._update_readouts(sample, receiver, k, scan)

    def _update_readouts(self, sample: float | None, receiver: float | None, k: int, scan: ScanSnapshot) -> None:
        src, n = self._source, len(self._plan)
        receiver_moving, sample_moving = src.large.snapshot()[1], src.small.snapshot()[1]
        if scan.active:
            activity = "scanning"
        elif receiver_moving or sample_moving:
            which = [name for name, m in (("receiver", receiver_moving), ("sample", sample_moving)) if m]
            activity = "moving " + " & ".join(which)
        else:
            activity = "done" if k >= n else "waiting"
        planned = self._plan[k] if k < n else None

        def angle(value: float | None) -> str:
            return "      ?" if value is None else f"{value:7.2f}°"

        phi1, phi2 = (None, None) if sample is None or receiver is None else geometry.phi_angles(sample, receiver)
        specular = sample is not None and receiver is not None and geometry.is_specular(sample, receiver)
        freq = f"{scan.freq:8.2f} GHz  {scan.points}/{scan.total}" if scan.active and scan.freq is not None else "-"
        elapsed = f"{clock.hms(clock.time() - src.t_start)}"
        if src.simulated:
            elapsed += f"  (x{clock.speed():g}, real {clock.hms(time.monotonic() - self._t0)})"
        lines = [
            f"Step         {min(k + 1, n)} of {n}  ({activity})",
            f"Sample R2    {angle(sample)}  {f'plan {planned.sample:7.2f}°' if planned else ''}",
            f"Receiver R1  {angle(receiver)}  {f'plan {planned.receiver:7.2f}°' if planned else ''}",
            f"φ1 incidence {angle(phi1)}",
            f"φ2 receiver  {angle(phi2)}  {'specular' if specular else ''}",
            f"Frequency    {freq}",
            f"{'Setup time' if src.simulated else 'Elapsed':<12} {elapsed}",
            f"File  {Path(self._files[k]).name if k < n else '-'}",
        ]
        self.details.config(text="\n".join(lines))
        self.progress["maximum"] = max(scan.total, 1)
        self.progress["value"] = scan.points if scan.active else (self.progress["maximum"] if k >= n else 0)

        warnings = []
        lo, hi = self._limits
        if receiver is not None and not lo - 0.01 <= receiver <= hi + 0.01:
            warnings.append(f"Receiver at {receiver:.2f}°, outside the soft limits {lo:g}°-{hi:g}°.")
        if (
            scan.active and planned and sample is not None and receiver is not None
            and (abs(sample - planned.sample) > 0.01 or abs(receiver - planned.receiver) > 0.01)
        ):
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

    def _draw(self, sample: float | None, receiver: float | None, k: int, scanning: bool) -> None:
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
            if i < k:
                fill, outline = _DONE, _DONE
            elif i == k:
                fill, outline = _CURRENT, _CURRENT
            else:
                fill, outline = "white", _BEAM
            if not lo <= step.receiver <= hi:
                outline = _BAD
            cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill=fill, outline=outline, width=2)
            if len(self._plan) <= 12:
                tx, ty = self._xy(step.receiver, R + 28)
                cv.create_text(tx, ty, text=str(i + 1), fill=_BEAM, font=("Helvetica", 8))

        # Tx (fixed) and the Tx beam
        cv.create_line(c, c, *self._xy(0, R), fill=_BEAM, width=2)
        self._box(0, R, 12, fill="white", outline=_RX, width=2)
        x, y = self._xy(0, R)
        cv.create_text(x, y - 24, text="Tx", fill=_RX, font=("Helvetica", 11, "bold"))

        if sample is not None:
            # sample normal, the sample on R2 perpendicular to it, and phi1
            cv.create_line(c, c, *self._xy(sample, R - 12), fill=_NORMAL, dash=(6, 4))
            self._arc(55, 0, sample, outline=_SAMPLE)
            x, y = self._xy(sample / 2, 70)
            cv.create_text(x, y, text=f"φ1 {sample:.1f}°", fill=_SAMPLE, font=("Helvetica", 9), anchor="w")
            (x0, y0), (x1, y1) = self._xy(sample + 90, 42), self._xy(sample - 90, 42)
            cv.create_line(x0, y0, x1, y1, fill=_SAMPLE, width=6)
            x, y = self._xy(sample - 90, 58)
            cv.create_text(x, y, text="Sample (R2)", fill=_SAMPLE, font=("Helvetica", 9, "bold"))
        cv.create_oval(c - 4, c - 4, c + 4, c + 4, fill="white", outline=_SAMPLE, width=2)

        if sample is not None and receiver is not None:
            if not geometry.is_specular(sample, receiver):  # where a specular receiver would be
                x, y = self._xy(2 * sample, R)
                cv.create_line(c, c, x, y, fill=_GHOST, dash=(2, 3))
                self._box(2 * sample, R, 10, fill="", outline=_GHOST, width=2)
                gx, gy = self._xy(2 * sample, R - 28)
                cv.create_text(gx, gy, text="specular", fill=_GHOST, font=("Helvetica", 8))
            self._arc(80, sample, receiver, outline=_SAMPLE)
            x, y = self._xy((sample + receiver) / 2, 95)
            cv.create_text(x, y, text=f"φ2 {receiver - sample:.1f}°", fill=_SAMPLE, font=("Helvetica", 9), anchor="w")

        if receiver is not None:
            cv.create_line(c, c, *self._xy(receiver, R), fill=_BEAM, width=2)
            moving = self._source.large.snapshot()[1]
            outside = not lo - 0.01 <= receiver <= hi + 0.01
            colour = _BAD if outside else (_RX_MOVING if moving else _RX)
            self._box(receiver, R, 12, fill=colour, outline=colour)
            x, y = self._xy(receiver, R - 30)
            cv.create_text(x, y, text="Rx", fill=colour, font=("Helvetica", 11, "bold"))
        if sample is None or receiver is None:
            cv.create_text(c, c + 30, text="stage position not known yet", fill=_BAD, font=("Helvetica", 9))

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


def live_window(source: SweepSource, plan: list[PlannedStep], files: list[str], stage_cfg: StageConfig):
    """A window function for run_with_emergency_stop(window=...)."""

    def show(estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
        LiveSweepWindow(estop, title, source, plan, files, stage_cfg).run(worker)

    return show


def _ago(t: float) -> str:
    seconds = max(0.0, clock.time() - t)
    return f"{seconds:.1f} s" if seconds < 60 else clock.hms(seconds)
