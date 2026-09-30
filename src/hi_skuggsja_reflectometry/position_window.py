"""Where are the arms right now? A live drawing of both arms, read from the
stages without moving them, with buttons to mirror and turn the drawing
until it looks like the table from where you stand, and to save that --
and, once allowed, buttons that turn the real arms a few degrees, for
calibrating."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import geometry
from .arm_mover import MAX_STEP
from .config import ConfigError, ViewConfig, save_view
from .hardware import NOT_HOMED_NOTE, ArmReading, PositionReader, arm_angle
from .live_view import SetupDrawing
from .stages import RECEIVER, SAMPLE, STAGE_LABELS
from .stop_window import open_root

if TYPE_CHECKING:
    from .arm_mover import ArmMover
    from .config import AppConfig
    from .sweeps import PlannedStep

_GREEN, _AMBER, _RED, _BLUE, _ORANGE = "#2e7d32", "#9a6200", "#c62828", "#1565c0", "#e65100"
DEFAULT_TURN_STEP = 5.0  # degrees the drawing turns
DEFAULT_MOVE_STEP = 1.0  # degrees an arm moves


def parse_turn_step(text: str) -> float | None:
    """The turn step typed in the window, in degrees (a decimal comma is
    fine), or None if it isn't a number between 0 and 360."""
    try:
        step = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return step if 0 < step <= 360 else None


@dataclass(frozen=True)
class PositionResult:
    confirmed: bool  # "the drawing matches the setup" was pressed
    readings: tuple[ArmReading, ArmReading] | None  # (receiver, sample) when confirmed
    view: ViewConfig  # the drawing as it was left, saved or not


class PositionWindow:
    """Shows where both arms are, from a PositionReader, and lets you make
    the drawing match the real setup: mirror it, turn it, and save that to
    the settings file.

    With a `mover` it can also turn the real arms, once "Allow moves" is
    ticked: a few degrees per click, the way the drawing shows it, with a
    STOP button. Leaving the window stops them. Without one it never moves
    anything.

    With `confirm` it is the first step before a real sweep: confirming
    returns the readings to simulate the sweep from; closing or cancelling
    returns none. Tk requires this to run on the main thread."""

    poll_ms = 150

    def __init__(
        self,
        reader: PositionReader,
        cfg: AppConfig,
        settings_file: Path,
        *,
        plan: Sequence[PlannedStep] = (),
        heading: str = "Where the arms are now",
        confirm: bool = False,
        simulated: bool = False,
        mover: ArmMover | None = None,
    ) -> None:
        tk, self.root = open_root("the position window", "Rerun with --no-gui to answer in the terminal instead.")
        self.tk = tk
        self._reader, self._stages, self._file = reader, cfg.stages, settings_file
        self._mover, self._simulated = mover, simulated
        self._plan = list(plan)
        self.view = self._saved_view = cfg.view
        self.result = PositionResult(False, None, cfg.view)

        self.root.title("reflecto - where the arms are (read only)")
        self.root.resizable(False, False)

        self.drawing = SetupDrawing(tk, self.root, cfg.stages, self.view)
        self.drawing.canvas.pack(side="left")
        panel = tk.Frame(self.root)
        panel.pack(side="right", fill="both", expand=True)

        self.banner = tk.Label(panel, fg="white", font=("Helvetica", 12, "bold"), pady=4)
        self.banner.pack(fill="x")
        tk.Label(panel, text=heading, font=("Helvetica", 12), wraplength=370, justify="center").pack(
            padx=16, pady=(12, 4)
        )
        self.readout = tk.Label(panel, font=("Courier", 10), justify="left", anchor="w")
        self.readout.pack(fill="x", padx=16, pady=4)
        self.notice = tk.Label(panel, font=("Helvetica", 10, "bold"), wraplength=370, justify="left", anchor="w")
        self.notice.pack(fill="x", padx=16)

        box = tk.LabelFrame(panel, text="Make the drawing look like the table from where you stand", padx=8, pady=6)
        box.pack(fill="x", padx=16, pady=(10, 4))
        row = tk.Frame(box)
        row.pack(fill="x")
        tk.Button(row, text="Mirror", width=7, command=self._mirror).pack(side="left")
        self.turns_label = tk.Label(row, anchor="w")
        self.turns_label.pack(side="left", padx=8)
        row = tk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="Turn by").pack(side="left")
        self.step_entry = tk.Entry(row, width=6, justify="right")
        self.step_entry.insert(0, f"{DEFAULT_TURN_STEP:g}")
        self.step_entry.pack(side="left", padx=(6, 2))
        tk.Label(row, text="°").pack(side="left")
        self.ccw_button = tk.Button(row, text="⟲ counterclockwise", command=lambda: self._turn_by_step(1))
        self.ccw_button.pack(side="left", padx=(10, 2))
        self.cw_button = tk.Button(row, text="⟳ clockwise", command=lambda: self._turn_by_step(-1))
        self.cw_button.pack(side="left", padx=2)
        self.direction_label = tk.Label(box, anchor="w", justify="left", font=("Helvetica", 9))
        self.direction_label.pack(fill="x", pady=(4, 0))
        row = tk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.save_button = tk.Button(row, text="Save", width=7, command=self._save)
        self.save_button.pack(side="left")
        self.save_status = tk.Label(row, anchor="w", justify="left", wraplength=270, font=("Helvetica", 9))
        self.save_status.pack(side="left", padx=8)

        self.allow_moves = tk.BooleanVar(master=self.root, value=False)
        self.move_buttons: list = []
        if mover is not None:
            self._build_move_box(panel)
        self._show_banner()

        actions = tk.Frame(panel)
        actions.pack(pady=(12, 14))
        self.ok_button = None
        if confirm:
            self.ok_button = tk.Button(
                actions, text="The drawing matches the setup:\nsimulate the sweep from here",
                bg=_GREEN, fg="white", activebackground=_GREEN, activeforeground="white",
                disabledforeground="#c8e6c9", font=("Helvetica", 11, "bold"), command=self._accept,
            )
            self.ok_button.pack(side="left", padx=4, ipady=4)
        tk.Button(
            actions, text="Cancel" if confirm else "Close", font=("Helvetica", 11), width=8, command=self._cancel
        ).pack(side="left", padx=4, ipady=4)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.root.bind("<Escape>", lambda _event: self._cancel())
        self._show_save_state()
        self._show_direction()
        self._refresh()

    # -- showing the arms ----------------------------------------------------------

    def _refresh(self) -> None:
        readings, errors = self._reader.latest()
        receiver = arm_angle(readings[0], True, self._stages) if readings[0] else None
        sample = arm_angle(readings[1], False, self._stages) if readings[1] else None
        self.drawing.view = self.view
        self.drawing.draw(sample, receiver, self._plan, -1, receiver_moving=bool(readings[0] and readings[0].moving))

        lines = []
        for name, reading, angle in (("Receiver R1", readings[0], receiver), ("Sample R2", readings[1], sample)):
            if reading is None:
                lines.append(f"{name:<13}not read")
                continue
            state = "homed" if reading.homed else "NOT HOMED"
            lines.append(f"{name:<13}{angle:8.2f}°  {state}{', moving' if reading.moving else ''}")
            lines.append(f"{'':<13}controller at {reading.position:.2f}°")
        if receiver is not None and sample is not None:
            phi1, phi2 = geometry.phi_angles(sample, receiver)
            lines.append(f"φ1 incidence {phi1:8.2f}°")
            lines.append(f"φ2 receiver  {phi2:8.2f}°")
        self.readout.config(text="\n".join(lines))

        failing = [f"{name}: {error}" for name, error in zip(("Receiver R1", "Sample R2"), errors) if error]
        if failing:
            self.notice.config(text="Not answering. " + " ".join(failing), fg=_RED)
        elif any(r is not None and not r.homed for r in readings):
            self.notice.config(text=NOT_HOMED_NOTE, fg=_AMBER)
        elif any(r is not None and r.moving for r in readings):
            self.notice.config(text="A stage is moving. Wait until both have stopped.", fg=_AMBER)
        else:
            self.notice.config(text="")
        if self.ok_button is not None:
            ready = all(readings) and not failing and not any(r.moving for r in readings if r)
            self.ok_button.config(state="normal" if ready else "disabled")
        self.turns_label.config(text=f"Angles grow {self.view.receiver_turns} from Tx")
        self._update_move_controls()
        self.root.after(self.poll_ms, self._refresh)

    def _update_move_controls(self) -> None:
        if self._mover is None:
            return
        busy, message, ok = self._mover.state()
        if message and not self.move_problem:
            self.move_status.config(text=message, fg=_GREEN if ok else _RED)
        allowed = self.allow_moves.get()
        for button in self.move_buttons:
            button.config(state="normal" if allowed and busy is None else "disabled")
        self.stop_button.config(state="normal" if allowed else "disabled")

    def _show_direction(self, problem: str = "") -> None:
        if problem:
            self.direction_label.config(text=problem, fg=_RED)
            return
        self.direction_label.config(
            text=f"Tx is drawn at {self.view.tx_direction:g}° (0 right, 90 up, 180 left, 270 down).", fg="black"
        )

    def _show_banner(self) -> None:
        if self._mover is not None and self.allow_moves.get():
            text = "MOVES ALLOWED - simulated stages" if self._simulated else "MOVES ALLOWED - the real arms can move"
            self.banner.config(text=text, bg=_ORANGE)
            self.root.title("reflecto - where the arms are (MOVES ALLOWED)")
        else:
            text = "SIMULATED STAGES - nothing is connected" if self._simulated else "READ ONLY - nothing moves"
            self.banner.config(text=text, bg=_BLUE if self._simulated else _GREEN)
            self.root.title("reflecto - where the arms are (read only)")

    # -- turning the real arms -------------------------------------------------

    def _build_move_box(self, panel) -> None:
        tk = self.tk
        box = tk.LabelFrame(panel, text="Turn the real arms, to calibrate", padx=8, pady=6)
        box.pack(fill="x", padx=16, pady=(10, 4))
        row = tk.Frame(box)
        row.pack(fill="x")
        tk.Checkbutton(row, text="Allow moves", variable=self.allow_moves, command=self._allow_changed).pack(side="left")
        tk.Label(row, text="Move by").pack(side="left", padx=(12, 0))
        self.move_entry = tk.Entry(row, width=5, justify="right")
        self.move_entry.insert(0, f"{DEFAULT_MOVE_STEP:g}")
        self.move_entry.pack(side="left", padx=(6, 2))
        tk.Label(row, text=f"° (at most {MAX_STEP:g}°)").pack(side="left")
        self.arm_buttons = {}
        for stage in (RECEIVER, SAMPLE):
            row = tk.Frame(box)
            row.pack(fill="x", pady=(6, 0))
            tk.Label(row, text=STAGE_LABELS[stage], width=11, anchor="w").pack(side="left")
            ccw = tk.Button(row, text="⟲ counterclockwise", command=lambda s=stage: self._move(s, True))
            cw = tk.Button(row, text="⟳ clockwise", command=lambda s=stage: self._move(s, False))
            ccw.pack(side="left", padx=2)
            cw.pack(side="left", padx=2)
            self.arm_buttons[stage] = (ccw, cw)
            self.move_buttons += [ccw, cw]
        row = tk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.stop_button = tk.Button(
            row, text="STOP", width=7, bg=_RED, fg="white", activebackground="#8e0000", activeforeground="white",
            disabledforeground="#ef9a9a", font=("Helvetica", 12, "bold"), command=self._stop_moves,
        )
        self.stop_button.pack(side="left")
        self.move_status = tk.Label(row, anchor="w", justify="left", wraplength=280, font=("Helvetica", 9))
        self.move_status.pack(side="left", padx=8)
        self.move_problem = False
        tk.Label(
            box, font=("Helvetica", 9), justify="left", anchor="w", wraplength=390,
            text="Each arm turns the way the drawing shows it. If the real arm turns the other way, press "
            "Mirror. Closing this window stops both stages.",
        ).pack(fill="x", pady=(6, 0))

    def _allow_changed(self) -> None:
        if not self.allow_moves.get():
            self._mover.stop()
        self._show_banner()
        self._update_move_controls()

    def _move(self, stage: str, counterclockwise: bool) -> None:
        if not self.allow_moves.get():
            return
        step = parse_turn_step(self.move_entry.get())
        if step is None:
            self._move_problem(f"Type how far to move in degrees, more than 0 and at most {MAX_STEP:g}.")
            return
        # the drawing shows angles growing one way; turn the arm that way on screen
        grows_ccw = self.view.receiver_turns == "counterclockwise"
        delta = step if counterclockwise == grows_ccw else -step
        readings, errors = self._reader.latest()
        index = 0 if stage == RECEIVER else 1
        if errors[index]:
            self._move_problem(f"{STAGE_LABELS[stage]} is not answering, so it isn't moved.")
            return
        problem = self._mover.move(stage, delta, readings[index])
        if problem:
            self._move_problem(problem)
        else:
            self.move_problem = False
        self._update_move_controls()

    def _move_problem(self, text: str) -> None:
        self.move_problem = True
        self.move_status.config(text=text, fg=_RED)

    def _stop_moves(self) -> None:
        self._mover.stop()
        self.move_problem = False

    # -- matching the drawing to the setup --------------------------------------

    def _mirror(self) -> None:
        turns = "counterclockwise" if self.view.receiver_turns == "clockwise" else "clockwise"
        self._set_view(replace(self.view, receiver_turns=turns))

    def _turn(self, degrees: float) -> None:
        self._set_view(replace(self.view, tx_direction=(self.view.tx_direction + degrees) % 360))

    def _turn_by_step(self, sign: int) -> None:
        step = parse_turn_step(self.step_entry.get())
        if step is None:
            self._show_direction("Type the turn in degrees, more than 0 and at most 360, e.g. 2.5.")
            return
        self._turn(sign * step)

    def _set_view(self, view: ViewConfig) -> None:
        self.view = view
        self.drawing.view = view
        self._show_save_state()
        self._show_direction()

    def _show_save_state(self) -> None:
        if self.view == self._saved_view:
            self.save_status.config(text="As saved.", fg=_GREEN)
        else:
            self.save_status.config(text="Changed: used for this run only until you save it.", fg=_AMBER)

    def _save(self) -> None:
        try:
            save_view(self._file, self.view)
        except (ConfigError, OSError) as e:
            self.save_status.config(text=f"Not saved: {e}", fg=_RED)
            return
        self._saved_view = self.view
        self.save_status.config(text=f"Saved in {self._file}", fg=_GREEN)

    # -- leaving -------------------------------------------------------------------

    def _leave(self) -> None:
        if self._mover is not None:
            self._mover.stop()  # a controller carries on with a move after the program lets go
        self.root.destroy()

    def _accept(self) -> None:
        readings, errors = self._reader.latest()
        if not all(readings) or any(errors) or any(r.moving for r in readings if r):
            return  # the button is disabled then; this covers a click in between
        self.result = PositionResult(True, (readings[0], readings[1]), self.view)
        self._leave()

    def _cancel(self) -> None:
        self.result = PositionResult(False, None, self.view)
        self._leave()

    def run(self) -> PositionResult:
        self.root.mainloop()
        return self.result
