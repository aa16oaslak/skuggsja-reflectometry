"""Drives the real Tk window; skipped when there is no display to open it on."""
import threading
import time

import pytest

from hi_skuggsja_reflectometry import stop_window
from hi_skuggsja_reflectometry.emergency_stop import EmergencyStop
from tests.fakes import FakeAxis


def make_window(estop):
    try:
        return stop_window.StopWindow(estop, "Test sweep")
    except stop_window.StopWindowUnavailable as exc:
        pytest.skip(f"no display for Tk: {exc}")


def sweep_that_runs_until_stopped(estop, linger=0.0):
    """Behaves like a sweep: keeps going until the stop event is set, then
    takes `linger` seconds to finish its current step."""

    def work():
        estop.event.wait(timeout=10)
        time.sleep(linger)

    return threading.Thread(target=work, daemon=True)


def test_clicking_stop_triggers_estop_and_shows_stopping_state():
    estop = EmergencyStop([FakeAxis(), FakeAxis()], ["a", "b"])
    window = make_window(estop)
    seen = {}

    def click_and_look():
        window.button.invoke()
        seen["text"] = window.button.cget("text")
        seen["state"] = str(window.button.cget("state"))

    window.root.after(200, click_and_look)

    window.run(sweep_that_runs_until_stopped(estop, linger=0.3))  # returns once the window has closed

    assert estop.reason == "STOP button"
    assert seen == {"text": "STOPPING", "state": "disabled"}


def test_closing_the_window_stops_the_sweep():
    estop = EmergencyStop([FakeAxis()], ["a"])
    window = make_window(estop)
    close_cmd = window.root.protocol("WM_DELETE_WINDOW")
    window.root.after(200, lambda: window.root.tk.call(close_cmd))

    window.run(sweep_that_runs_until_stopped(estop))

    assert estop.reason == "stop window closed"


def test_esc_in_the_window_stops_the_sweep():
    estop = EmergencyStop([FakeAxis()], ["a"])
    window = make_window(estop)
    got_focus = []

    def focus_and_press_esc():
        window.root.focus_force()  # key events only reach the focused window
        window.root.update()
        got_focus.append(window.root.focus_get() is not None)
        if got_focus[0]:
            window.root.event_generate("<Escape>", when="tail")
        else:
            estop.trigger("test cleanup")  # end the run; the skip below explains why

    window.root.after(200, focus_and_press_esc)

    window.run(sweep_that_runs_until_stopped(estop))

    if not got_focus[0]:
        pytest.skip("the desktop refused keyboard focus to the window (e.g. a CI runner)")
    assert estop.reason == "Esc in stop window"


def test_window_closes_by_itself_when_the_sweep_finishes():
    estop = EmergencyStop([FakeAxis()], ["a"])
    window = make_window(estop)

    window.run(threading.Thread(target=lambda: time.sleep(0.2), daemon=True))

    assert not estop.event.is_set()  # finished normally, no stop sent
