from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .stop_window import run_stop_window

if TYPE_CHECKING:
    import libximc.highlevel as ximc

Waiter = Callable[["EmergencyStop", threading.Thread, str], None]


class EmergencyStop:
    """The stop_event every blocking wait in the sweep checks, plus the act
    of halting the stages the moment it is set.

    trigger() never blocks -- the stop commands go out from a short-lived
    thread -- so it is safe to call from the GUI, a Ctrl+C handler, or the
    sweep thread itself."""

    def __init__(self, axes: Sequence[ximc.Axis], names: Sequence[str]) -> None:
        self.event = threading.Event()
        self.reason: str | None = None
        self._axes = list(axes)
        self._names = list(names)

    def trigger(self, reason: str) -> None:
        # Two racing callers can both get past this check; the only cost is
        # a second command_stop(), which is harmless.
        if self.event.is_set():
            return
        self.reason = reason
        self.event.set()
        print(f"\n🛑 STOP ({reason}) -- halting stages")
        threading.Thread(target=self._stop_axes, daemon=True).start()

    def _stop_axes(self) -> None:
        for axis, name in zip(self._axes, self._names):
            try:
                axis.command_stop()
                print(f"  [{name}] stopped ✓")
            except Exception as e:  # noqa: BLE001 -- one failed stop must not skip the other stage
                print(f"  [{name}] stop failed: {e}")


def _route_ctrl_c_to(estop: EmergencyStop) -> Callable[[], None]:
    """Makes Ctrl+C in the terminal trigger the stop for the duration of the
    sweep. Returns a function that restores the previous handler."""
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: estop.trigger("Ctrl+C"))
    return lambda: signal.signal(signal.SIGINT, previous)


def _join(worker: threading.Thread) -> None:
    # join() in short slices: a bare join() blocks Ctrl+C on Windows.
    while worker.is_alive():
        worker.join(timeout=0.2)


def wait_in_console(estop: EmergencyStop, worker: threading.Thread, title: str) -> None:
    print(f"{title}\nPress Ctrl+C to stop.")
    worker.start()
    _join(worker)


def _close_axes(axes: Sequence[ximc.Axis], names: Sequence[str]) -> None:
    print("\nClosing connections...")
    for axis, name in zip(axes, names):
        try:
            axis.close_device()
            print(f"  [{name}] disconnected ✓")
        except Exception as e:  # noqa: BLE001 -- keep closing the remaining stages
            print(f"  [{name}] disconnect failed: {e}")


def run_with_emergency_stop(
    sweep_fn: Callable[..., None],
    axes: Sequence[ximc.Axis],
    names: Sequence[str],
    *sweep_args,
    gui: bool = True,
    title: str = "Sweep running",
    window: Waiter | None = None,
) -> None:
    """Runs sweep_fn(*sweep_args, stop_event) on a worker thread while the
    main thread shows the STOP window -- or `window`, if given -- or, with
    gui=False, just waits, with Ctrl+C as the stop.

    However the sweep ends -- normally, by a stop, or by an unexpected
    exception -- the stages are halted if needed and then closed.
    RuntimeErrors are reported as aborts (that's how a stop ends a stage
    wait); any other exception is re-raised here after cleanup."""
    estop = EmergencyStop(axes, names)
    failure: list[BaseException] = []

    def work() -> None:
        try:
            sweep_fn(*sweep_args, estop.event)
        except BaseException as e:  # noqa: BLE001 -- any failure must halt the stages
            if estop.event.is_set():
                print(f"\n🛑 Aborted: {e}")
            else:
                estop.trigger(f"{type(e).__name__}: {e}")
            if not isinstance(e, RuntimeError):
                failure.append(e)

    waiter: Waiter = (window or run_stop_window) if gui else wait_in_console
    worker = threading.Thread(target=work, name="sweep", daemon=True)
    restore_ctrl_c = _route_ctrl_c_to(estop)
    try:
        waiter(estop, worker, title)
    finally:
        if worker.is_alive():  # the waiter bailed out early, e.g. the window crashed
            estop.trigger("stop window failed")
            _join(worker)
        restore_ctrl_c()
        _close_axes(axes, names)

    if failure:
        raise failure[0]
