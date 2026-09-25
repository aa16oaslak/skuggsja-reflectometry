from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .emergency_stop import EmergencyStop

_RED, _DARK_RED, _GREY = "#c62828", "#8e0000", "#616161"


class StopWindowUnavailable(RuntimeError):
    """Raised when the stop window can't be shown: tkinter missing, or no
    display to open it on."""


class StopWindow:
    """Always-on-top window with a big STOP button, shown for as long as the
    sweep thread runs.

    The window never talks to hardware itself: every way of stopping (the
    button, Esc while the window is focused, closing the window) only calls
    estop.trigger(), which returns immediately, so the window stays
    responsive even while the sweep thread is blocked in a stage or TOptica
    call. Tk requires this to be created and run on the main thread.

    Subclasses can add widgets to the left of `self.controls`, or into it
    before `self.button`, and extend _refresh(), which runs every poll_ms."""

    poll_ms = 100
    window_title = "reflecto - emergency stop"

    def __init__(self, estop: EmergencyStop, title: str) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            raise StopWindowUnavailable(
                "tkinter is not available in this Python, so the stop window can't open.\n"
                "Rerun with --no-gui to use Ctrl+C in the terminal instead."
            ) from exc
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            raise StopWindowUnavailable(
                "Could not open the stop window (no display?).\n"
                "Rerun with --no-gui to use Ctrl+C in the terminal instead.\n"
                f"Original error: {exc}"
            ) from exc

        self.tk = tk
        self._estop = estop
        self._title = title
        self._t0 = time.monotonic()

        self.root.title(self.window_title)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        self.controls = tk.Frame(self.root)
        self.controls.pack(side="right", fill="both", expand=True)

        self.status = tk.Label(self.controls, text=title, font=("Helvetica", 12), wraplength=400, justify="center")
        self.status.pack(padx=16, pady=(14, 6))

        self.button = tk.Button(
            self.controls,
            text="STOP",
            font=("Helvetica", 40, "bold"),
            bg=_RED,
            fg="white",
            activebackground=_DARK_RED,
            activeforeground="white",
            disabledforeground="white",
            bd=4,
            relief="raised",
            width=8,
            command=lambda: self._stop("STOP button"),
        )
        self.button.pack(fill="both", expand=True, padx=16, pady=6, ipady=18)

        tk.Label(
            self.controls,
            text="Esc also stops while this window is focused.\nClosing this window stops the sweep.",
            font=("Helvetica", 9),
        ).pack(padx=16, pady=(6, 14))

        self.root.bind("<Escape>", lambda _event: self._stop("Esc in stop window"))
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._stop("stop window closed"))

    def _stop(self, reason: str) -> None:
        self._estop.trigger(reason)
        self._refresh()

    def _refresh(self) -> None:
        if self._estop.event.is_set():
            self.button.config(text="STOPPING", state="disabled", bg=_GREY, relief="sunken")
            self.status.config(
                text=f"Stop sent to both stages ({self._estop.reason}).\n"
                "Finishing the current step and saving data...\n"
                "(If the terminal is asking a question, answer it there.)"
            )
        else:
            elapsed = int(time.monotonic() - self._t0)
            h, m, s = elapsed // 3600, elapsed // 60 % 60, elapsed % 60
            self.status.config(text=f"{self._title}\nRunning {h}:{m:02d}:{s:02d}")

    def _poll(self) -> None:
        if not self._worker.is_alive():
            self.root.destroy()
            return
        self._refresh()
        self.root.after(self.poll_ms, self._poll)

    def run(self, worker: threading.Thread) -> None:
        """Starts `worker` once the window is on screen, and blocks until
        the worker finishes; the window then closes itself."""
        self._worker = worker
        self.root.update()  # draw the button before anything can move
        worker.start()
        self.root.after(self.poll_ms, self._poll)
        self.root.mainloop()


def run_stop_window(estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
    StopWindow(estop, title).run(worker)
