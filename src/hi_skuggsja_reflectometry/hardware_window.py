"""Status lights for the hardware: a panel used in the sweep window, and the
`reflecto check` window built around it."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .hardware import FAIL, OK, UNKNOWN, WARN, DeviceStatus
from .stop_window import open_root

LIGHT = {OK: "#2e7d32", WARN: "#f9a825", FAIL: "#c62828", UNKNOWN: "#9e9e9e"}


class HardwarePanel:
    """One row per device: a coloured light, name and address, and a
    one-line summary; with details=True also the details under it."""

    def __init__(self, tk: Any, parent: Any, details: bool, width: int = 340) -> None:
        self._tk, self._details, self._width = tk, details, width
        self.frame = tk.Frame(parent)
        self._rows: list[tuple[Any, int, Any, Any, Any]] = []

    def _add_row(self) -> None:
        tk = self._tk
        row = tk.Frame(self.frame)
        row.pack(fill="x", pady=2)
        light = tk.Canvas(row, width=18, height=18, highlightthickness=0)
        light.pack(side="left", anchor="n", padx=(0, 8), pady=2)
        dot = light.create_oval(2, 2, 16, 16, outline="")
        text = tk.Frame(row)
        text.pack(side="left", fill="x", expand=True)
        title = tk.Label(text, font=("Helvetica", 10, "bold"), anchor="w", justify="left")
        title.pack(fill="x")
        summary = tk.Label(text, font=("Helvetica", 9), anchor="w", justify="left", wraplength=self._width)
        summary.pack(fill="x")
        details = tk.Label(text, font=("Helvetica", 8), fg="#616161", anchor="w", justify="left",
                           wraplength=self._width)
        if self._details:
            details.pack(fill="x")
        self._rows.append((light, dot, title, summary, details))

    def show(self, statuses: list[DeviceStatus]) -> None:
        while len(self._rows) < len(statuses):
            self._add_row()
        for (light, dot, title, summary, details), status in zip(self._rows, statuses):
            light.itemconfig(dot, fill=LIGHT[status.state])
            title.config(text=f"{status.name}  ({status.address})")
            summary.config(text=status.summary)
            details.config(text="\n".join(status.details))


class HardwareCheckWindow:
    """The `reflecto check` window. `check` runs on a background thread, so
    the window stays responsive while a device takes its time to answer."""

    def __init__(self, check: Callable[[], list[DeviceStatus]], checking: list[DeviceStatus]) -> None:
        tk, self.root = open_root("the hardware check window", "Rerun with --no-gui to print the result instead.")
        self._check = check
        self._checking = checking  # rows to show while a check runs
        self.result: list[DeviceStatus] | None = None
        self._pending: list[DeviceStatus] | BaseException | None = None
        self._thread: threading.Thread | None = None

        self.root.title("reflecto - hardware check")
        self.root.resizable(False, False)
        tk.Label(self.root, text="Hardware check", font=("Helvetica", 14, "bold")).pack(padx=16, pady=(14, 0))
        tk.Label(self.root, text="Reads status only: nothing moves and no setting changes.",
                 font=("Helvetica", 9), fg="#616161").pack(padx=16)
        self.panel = HardwarePanel(tk, self.root, details=True, width=420)
        self.panel.frame.pack(fill="x", padx=16, pady=10)
        buttons = tk.Frame(self.root)
        buttons.pack(pady=(0, 6))
        self.again = tk.Button(buttons, text="Check again", width=12, command=self.start_check)
        self.again.pack(side="left", padx=4)
        tk.Button(buttons, text="Close", width=12, command=self.root.destroy).pack(side="left", padx=4)
        self.footer = tk.Label(self.root, font=("Helvetica", 9), fg="#616161")
        self.footer.pack(pady=(0, 12))

    def start_check(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.again.config(state="disabled")
        self.footer.config(text="Checking...")
        self.panel.show(self._checking)
        self._pending = None

        def work() -> None:
            try:
                self._pending = self._check()
            except BaseException as e:  # noqa: BLE001 -- shown in the window instead of lost in a thread
                self._pending = e

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self.root.after(100, self._poll)

    def _poll(self) -> None:
        if self._thread and self._thread.is_alive():
            self.root.after(100, self._poll)
            return
        self.again.config(state="normal")
        if isinstance(self._pending, BaseException):
            self.footer.config(text=f"The check itself failed: {self._pending}")
            return
        self.result = self._pending
        self.panel.show(self.result)
        self.footer.config(text=f"Last checked {time.strftime('%H:%M:%S')}")

    def run(self) -> list[DeviceStatus] | None:
        """Shows the window and checks once; returns the latest result when
        the window is closed."""
        self.start_check()
        self.root.mainloop()
        return self.result
