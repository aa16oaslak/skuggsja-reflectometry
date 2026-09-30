"""Live top-down view of the setup during a sweep, real or simulated, drawn
like Fig. 1 of Singh et al., arXiv:2407.05512, next to the STOP button and
the hardware status lights.

How the drawing is turned and which way angles grow come from the [view]
settings, so it can be made to look like the table from where you stand;
`reflecto position` sets them against the real arms. The receiver (Rx)
moves on the R1 ring; the sample turns on R2 in the middle.

Everything shown comes from the sweep's own communication with the
hardware (see SweepSource); the window never talks to the hardware."""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import clock, geometry, toptica
from .hardware import FAIL, OK, WARN, DeviceStatus
from .hardware_window import HardwarePanel
from .stop_window import StopWindow

if TYPE_CHECKING:
    import threading

    from .config import AppConfig, StageConfig, ViewConfig
    from .emergency_stop import EmergencyStop
    from .hardware import WatchedAxis
    from .monitoring import ScanSnapshot
    from .sweeps import PlannedStep

_BEAM, _RING, _RANGE = "#757575", "#9e9e9e", "#424242"
_SAMPLE, _NORMAL, _GHOST = "#6d4c41", "#8d6e63", "#bdbdbd"
_RX, _RX_MOVING, _BAD = "#37474f", "#1565c0", "#c62828"
_DONE, _CURRENT = "#2e7d32", "#ef6c00"
_SIM_BANNER, _REAL_BANNER = "#1565c0", "#e65100"

Verdict = Callable[[], "tuple[bool, list[str]]"]


def _sense(view: ViewConfig) -> int:
    """+1 if angles grow counterclockwise on screen, -1 if clockwise."""
    return 1 if view.receiver_turns == "counterclockwise" else -1


def screen_point(angle: float, radius: float, centre: float, view: ViewConfig) -> tuple[float, float]:
    """Canvas (x, y) of the point `radius` from the centre at `angle` degrees
    from the Tx direction, drawn as `view` says. Canvas y grows downwards."""
    a = math.radians(view.tx_direction + _sense(view) * angle)
    return centre + radius * math.cos(a), centre - radius * math.sin(a)


def arc_angles(start: float, end: float, view: ViewConfig) -> tuple[float, float]:
    """Tk's (start, extent) for the arc from `start` to `end` degrees from Tx.
    Tk measures counterclockwise on screen; a negative extent runs clockwise."""
    s = _sense(view)
    return view.tx_direction + s * start, s * (end - start)


class SetupDrawing:
    """The setup seen from above on a Tk canvas: Tx, the R1 ring with the
    receiver's allowed range, the receiver, the sample on R2 with its normal,
    the angles phi1 and phi2, and the receiver positions a sweep will scan
    at. Change `view` and redraw to turn or mirror it."""

    SIZE = 540  # canvas, px
    RING = 205  # R1 ring radius, px

    def __init__(self, tk: Any, parent: Any, stage_cfg: StageConfig, view: ViewConfig) -> None:
        self.canvas = tk.Canvas(parent, width=self.SIZE, height=self.SIZE, bg="white", highlightthickness=0)
        self.view = view
        self._limits = (stage_cfg.angle_min, stage_cfg.angle_max)
        self._receiver_home = geometry.receiver_angle(0.0, stage_cfg.zero_l)

    def _xy(self, angle: float, radius: float) -> tuple[float, float]:
        return screen_point(angle, radius, self.SIZE / 2, self.view)

    def _outward(self, x: float, y: float) -> str:
        """Text anchor that puts a label at (x, y) on the side away from the
        centre, so it doesn't run back over the lines it labels."""
        dx, dy = x - self.SIZE / 2, y - self.SIZE / 2
        if abs(dx) >= abs(dy):
            return "w" if dx > 0 else "e"
        return "n" if dy > 0 else "s"

    def _box(self, angle: float, radius: float, half: float, **style: Any) -> None:
        """A square on the ring, turned to face the centre."""
        x, y = self._xy(angle, radius)
        c = self.SIZE / 2
        ux, uy = (x - c) / radius, (y - c) / radius  # unit vector from the centre
        vx, vy = -uy, ux
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        points = [coord for sx, sy in corners for coord in (x + (sx * ux + sy * vx) * half, y + (sx * uy + sy * vy) * half)]
        self.canvas.create_polygon(*points, **style)

    def _arc(self, radius: float, start: float, end: float, **style: Any) -> None:
        c = self.SIZE / 2
        tk_start, tk_extent = arc_angles(start, end, self.view)
        self.canvas.create_arc(
            c - radius, c - radius, c + radius, c + radius,
            start=tk_start, extent=tk_extent, style="arc", **style,
        )

    def _label(self, angle: float, radius: float, text: str, **style: Any) -> None:
        x, y = self._xy(angle, radius)
        self.canvas.create_text(x, y, text=text, anchor=self._outward(x, y), **style)

    def draw(
        self,
        sample: float | None,
        receiver: float | None,
        plan: Sequence[PlannedStep] = (),
        step: int = -1,
        receiver_moving: bool = False,
    ) -> None:
        """Redraws everything. `step` is the index of the plan step being
        scanned or moved to (earlier ones show as scanned); -1 for none."""
        cv, R, c = self.canvas, self.RING, self.SIZE / 2
        cv.delete("all")
        lo, hi = self._limits

        # R1 ring: dotted, solid over the receiver's allowed range
        cv.create_oval(c - R, c - R, c + R, c + R, outline=_RING, dash=(2, 4), width=2)
        self._arc(R, lo, hi, outline=_RANGE, width=5)
        hx, hy = self._xy(self._receiver_home, R + 16)
        cv.create_text(hx, hy, text="home", fill=_RING, font=("Helvetica", 8))

        # planned receiver positions
        for i, planned in enumerate(plan):
            x, y = self._xy(planned.receiver, R + 14)
            if 0 <= step and i < step:
                fill, outline = _DONE, _DONE
            elif i == step:
                fill, outline = _CURRENT, _CURRENT
            else:
                fill, outline = "white", _BEAM
            if not lo <= planned.receiver <= hi:
                outline = _BAD
            cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill=fill, outline=outline, width=2)
            if len(plan) <= 12:
                tx, ty = self._xy(planned.receiver, R + 28)
                cv.create_text(tx, ty, text=str(i + 1), fill=_BEAM, font=("Helvetica", 8))

        # Tx (fixed) and the Tx beam
        cv.create_line(c, c, *self._xy(0, R), fill=_BEAM, width=2)
        self._box(0, R, 12, fill="white", outline=_RX, width=2)
        self._label(0, R + 18, "Tx", fill=_RX, font=("Helvetica", 11, "bold"))

        if sample is not None:
            # sample normal, the sample on R2 perpendicular to it, and phi1
            cv.create_line(c, c, *self._xy(sample, R - 12), fill=_NORMAL, dash=(6, 4))
            self._arc(55, 0, sample, outline=_SAMPLE)
            self._label(sample / 2, 62, f"φ1 {sample:.1f}°", fill=_SAMPLE, font=("Helvetica", 9))
            (x0, y0), (x1, y1) = self._xy(sample + 90, 42), self._xy(sample - 90, 42)
            cv.create_line(x0, y0, x1, y1, fill=_SAMPLE, width=6)
            self._label(sample - 90, 48, "Sample (R2)", fill=_SAMPLE, font=("Helvetica", 9, "bold"))
        cv.create_oval(c - 4, c - 4, c + 4, c + 4, fill="white", outline=_SAMPLE, width=2)

        if sample is not None and receiver is not None:
            if not geometry.is_specular(sample, receiver):  # where a specular receiver would be
                x, y = self._xy(2 * sample, R)
                cv.create_line(c, c, x, y, fill=_GHOST, dash=(2, 3))
                self._box(2 * sample, R, 10, fill="", outline=_GHOST, width=2)
                gx, gy = self._xy(2 * sample, R - 28)
                cv.create_text(gx, gy, text="specular", fill=_GHOST, font=("Helvetica", 8))
            self._arc(80, sample, receiver, outline=_SAMPLE)
            self._label((sample + receiver) / 2, 87, f"φ2 {receiver - sample:.1f}°", fill=_SAMPLE, font=("Helvetica", 9))

        if receiver is not None:
            cv.create_line(c, c, *self._xy(receiver, R), fill=_BEAM, width=2)
            outside = not lo - 0.01 <= receiver <= hi + 0.01
            colour = _BAD if outside else (_RX_MOVING if receiver_moving else _RX)
            self._box(receiver, R, 12, fill=colour, outline=colour)
            x, y = self._xy(receiver, R - 30)
            cv.create_text(x, y, text="Rx", fill=colour, font=("Helvetica", 11, "bold"))
        if sample is None or receiver is None:
            cv.create_text(c, c + 30, text="stage position not known yet", fill=_BAD, font=("Helvetica", 9))

        # legend
        cv.create_line(12, 16, 40, 16, fill=_RANGE, width=5)
        cv.create_text(46, 16, text=f"receiver range (soft limits {lo:g}°-{hi:g}°)", anchor="w",
                       fill=_RANGE, font=("Helvetica", 9))
        if plan:
            for j, (fill, label) in enumerate(((_DONE, "scanned"), (_CURRENT, "current"), ("white", "planned"))):
                y = 36 + 16 * j
                cv.create_oval(21, y - 5, 31, y + 5, fill=fill, outline=_BEAM if fill == "white" else fill, width=2)
                cv.create_text(46, y, text=label, anchor="w", fill=_RANGE, font=("Helvetica", 9))
        cv.create_text(self.SIZE - 8, self.SIZE - 24, anchor="e", fill=_RANGE, font=("Helvetica", 8),
                       text=f"Seen from above. Angles grow {self.view.receiver_turns} from Tx.")
        cv.create_text(self.SIZE - 8, self.SIZE - 10, anchor="e", fill=_GHOST, font=("Helvetica", 8),
                       text="After Fig. 1 of Singh et al., arXiv:2407.05512")


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
    and readouts: stage angles against the plan, phi1/phi2, scan progress.

    With a `verdict` (a simulation run before the real sweep), the window
    stays open when the sweep ends, shows the verdict, and asks whether to
    run the sweep on the real hardware; the answer is left in `decision`."""

    poll_ms = 50

    def __init__(
        self,
        estop: EmergencyStop,
        title: str,
        source: SweepSource,
        plan: list[PlannedStep],
        files: list[str],
        cfg: AppConfig,
        verdict: Verdict | None = None,
    ) -> None:
        self.window_title = (
            "reflecto - SIMULATION (no hardware)" if source.simulated else "reflecto - sweep (real hardware)"
        )
        super().__init__(estop, title)
        tk = self.tk
        from tkinter import ttk

        self._source, self._plan, self._files = source, plan, files
        self._limits = (cfg.stages.angle_min, cfg.stages.angle_max)
        self._verdict = verdict
        self.decision: bool | None = None

        if verdict is not None:
            banner = ("SIMULATION before the real sweep - nothing moves yet", _SIM_BANNER)
        elif source.simulated:
            banner = ("SIMULATION - no hardware is used", _SIM_BANNER)
        else:
            banner = ("REAL HARDWARE - the stages will move", _REAL_BANNER)
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

        self.drawing = SetupDrawing(tk, self.root, cfg.stages, cfg.view)
        self.drawing.canvas.pack(side="left")
        self._refresh()

    # -- state -------------------------------------------------------------------

    def _step(self, scan: ScanSnapshot) -> int:
        """Index of the plan step being scanned, or moved to."""
        return scan.started - 1 if scan.active else scan.started

    def _refresh(self) -> None:
        super()._refresh()
        if not self._estop.event.is_set():
            self.status.config(text=self._title)  # elapsed time is in the readouts
        sample, receiver = self._source.angles()
        scan = self._source.scan()
        k = self._step(scan)
        self.hardware.show(self._source.devices())
        self.drawing.draw(sample, receiver, self._plan, k, receiver_moving=self._source.large.snapshot()[1])
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

    # -- the decision after a simulation run before the real sweep ---------------

    def _poll(self) -> None:
        if self._verdict is not None and not self._worker.is_alive():
            self._refresh()  # the final state
            self._ask_to_run()
            return
        super()._poll()

    def _ask_to_run(self) -> None:
        tk = self.tk
        ok, lines = self._verdict()
        if self._estop.event.is_set():  # STOP, or an error, ended it early
            ok = False
            lines = [f"The simulation did not finish ({self._estop.reason}).", *lines]
        self.status.config(text="Simulation finished." if ok else "The simulation found a problem.")
        self.button.pack_forget()
        self.hint.config(text="Esc or closing this window cancels: nothing will move.")
        box = tk.Frame(self.controls)
        box.pack(fill="x", padx=16, pady=6, before=self.hint)
        self.verdict_text = tk.Label(
            box, text="\n".join(lines), justify="left", anchor="w", wraplength=340,
            fg=_DONE if ok else _BAD, font=("Helvetica", 10, "bold"),
        )
        self.verdict_text.pack(fill="x")
        buttons = tk.Frame(box)
        buttons.pack(pady=(10, 0))
        self.run_button = None
        if ok:
            self.run_button = tk.Button(
                buttons, text="Run on the real hardware", bg=_REAL_BANNER, fg="white",
                activebackground=_REAL_BANNER, activeforeground="white", font=("Helvetica", 13, "bold"),
                command=lambda: self._decide(True),
            )
            self.run_button.pack(side="left", padx=4, ipady=6)
        self.cancel_button = tk.Button(
            buttons, text="Cancel" if ok else "Close", font=("Helvetica", 12), command=lambda: self._decide(False)
        )
        self.cancel_button.pack(side="left", padx=4, ipady=6)
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._decide(False))
        self.root.bind("<Escape>", lambda _event: self._decide(False))

    def _decide(self, run: bool) -> None:
        self.decision = run
        self.root.destroy()


class LiveWindow:
    """A window function for run_with_emergency_stop(window=...). With a
    `verdict`, the window asks at the end whether to run the sweep on the
    real hardware, and the answer is left in `decision`."""

    def __init__(
        self,
        source: SweepSource,
        plan: list[PlannedStep],
        files: list[str],
        cfg: AppConfig,
        verdict: Verdict | None = None,
    ) -> None:
        self._args = (source, plan, files, cfg, verdict)
        self.decision: bool | None = None

    def __call__(self, estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
        source, plan, files, cfg, verdict = self._args
        window = LiveSweepWindow(estop, title, source, plan, files, cfg, verdict)
        window.run(worker)
        self.decision = window.decision


def live_window(
    source: SweepSource, plan: list[PlannedStep], files: list[str], cfg: AppConfig, verdict: Verdict | None = None
) -> LiveWindow:
    return LiveWindow(source, plan, files, cfg, verdict)


def _ago(t: float) -> str:
    seconds = max(0.0, clock.time() - t)
    return f"{seconds:.1f} s" if seconds < 60 else clock.hms(seconds)
