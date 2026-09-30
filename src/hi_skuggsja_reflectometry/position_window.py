"""Where are the arms right now? A live drawing of both arms, read from the
stages without moving them, with buttons to mirror and turn the drawing
until it looks like the table from where you stand, and to save that."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import geometry
from .config import ConfigError, ViewConfig, save_view
from .hardware import NOT_HOMED_NOTE, ArmReading, PositionReader, arm_angle
from .live_view import SetupDrawing
from .stop_window import open_root

if TYPE_CHECKING:
    from .config import AppConfig
    from .sweeps import PlannedStep

_GREEN, _AMBER, _RED, _BLUE = "#2e7d32", "#9a6200", "#c62828", "#1565c0"


@dataclass(frozen=True)
class PositionResult:
    confirmed: bool  # "the drawing matches the setup" was pressed
    readings: tuple[ArmReading, ArmReading] | None  # (receiver, sample) when confirmed
    view: ViewConfig  # the drawing as it was left, saved or not


class PositionWindow:
    """Shows where both arms are, from a PositionReader, and lets you make
    the drawing match the real setup: mirror it, turn it, and save that to
    the settings file. Never moves anything.

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
    ) -> None:
        tk, self.root = open_root("the position window", "Rerun with --no-gui to answer in the terminal instead.")
        self._reader, self._stages, self._file = reader, cfg.stages, settings_file
        self._plan = list(plan)
        self.view = self._saved_view = cfg.view
        self.result = PositionResult(False, None, cfg.view)

        self.root.title("reflecto - where the arms are (read only)")
        self.root.resizable(False, False)

        self.drawing = SetupDrawing(tk, self.root, cfg.stages, self.view)
        self.drawing.canvas.pack(side="left")
        panel = tk.Frame(self.root)
        panel.pack(side="right", fill="both", expand=True)

        tk.Label(
            panel,
            text="SIMULATED STAGES - nothing is connected" if simulated else "READ ONLY - nothing moves",
            bg=_BLUE if simulated else _GREEN, fg="white", font=("Helvetica", 12, "bold"), pady=4,
        ).pack(fill="x")
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
        for label, sign in (("Turn counterclockwise", 1), ("Turn clockwise", -1)):
            row = tk.Frame(box)
            row.pack(fill="x", pady=(6, 0))
            tk.Label(row, text=label, width=20, anchor="w").pack(side="left")
            for degrees in (15, 90):
                tk.Button(
                    row, text=f"{degrees}°", width=4, command=lambda d=sign * degrees: self._turn(d)
                ).pack(side="left", padx=2)
        row = tk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.save_button = tk.Button(row, text="Save", width=7, command=self._save)
        self.save_button.pack(side="left")
        self.save_status = tk.Label(row, anchor="w", justify="left", wraplength=270, font=("Helvetica", 9))
        self.save_status.pack(side="left", padx=8)

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
        self.root.after(self.poll_ms, self._refresh)

    # -- matching the drawing to the setup --------------------------------------

    def _mirror(self) -> None:
        turns = "counterclockwise" if self.view.receiver_turns == "clockwise" else "clockwise"
        self._set_view(replace(self.view, receiver_turns=turns))

    def _turn(self, degrees: float) -> None:
        self._set_view(replace(self.view, tx_direction=self.view.tx_direction + degrees))

    def _set_view(self, view: ViewConfig) -> None:
        self.view = view
        self.drawing.view = view
        self._show_save_state()

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

    def _accept(self) -> None:
        readings, errors = self._reader.latest()
        if not all(readings) or any(errors) or any(r.moving for r in readings if r):
            return  # the button is disabled then; this covers a click in between
        self.result = PositionResult(True, (readings[0], readings[1]), self.view)
        self.root.destroy()

    def _cancel(self) -> None:
        self.result = PositionResult(False, None, self.view)
        self.root.destroy()

    def run(self) -> PositionResult:
        self.root.mainloop()
        return self.result
