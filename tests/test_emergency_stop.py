import signal

import pytest

from hi_skuggsja_reflectometry import emergency_stop
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.fakes import FakeAxis, ImmediateThread


@pytest.fixture(autouse=True)
def synchronous_threads(monkeypatch):
    # The sweep thread and the stop-command thread both run inline, so every
    # side effect has happened by the time the call under test returns.
    monkeypatch.setattr(emergency_stop.threading, "Thread", ImmediateThread)


def press_stop_then_start(estop, worker, title):
    """Stands in for the stop window: STOP is clicked before the sweep's
    first step."""
    estop.trigger("STOP button")
    worker.start()


def test_trigger_sets_event_and_stops_every_axis():
    axes = [FakeAxis(), FakeAxis()]
    estop = emergency_stop.EmergencyStop(axes, ["a", "b"])

    estop.trigger("STOP button")

    assert estop.event.is_set()
    assert estop.reason == "STOP button"
    assert all(axis.calls == [("stop",)] for axis in axes)


def test_trigger_twice_stops_once_and_keeps_first_reason():
    axes = [FakeAxis()]
    estop = emergency_stop.EmergencyStop(axes, ["a"])

    estop.trigger("STOP button")
    estop.trigger("Ctrl+C")

    assert estop.reason == "STOP button"
    assert axes[0].calls == [("stop",)]


def test_trigger_still_stops_other_axes_if_one_fails(capsys):
    class BrokenAxis(FakeAxis):
        def command_stop(self):
            raise RuntimeError("port gone")

    axes = [BrokenAxis(), FakeAxis()]

    emergency_stop.EmergencyStop(axes, ["a", "b"]).trigger("STOP button")

    assert "[a] stop failed: port gone" in capsys.readouterr().out
    assert axes[1].calls == [("stop",)]


def test_normal_completion_closes_each_axis_once_without_stopping():
    axes = [FakeAxis(), FakeAxis()]
    sweep_calls = []

    def sweep_fn(marker, stop_event):
        sweep_calls.append((marker, stop_event.is_set()))

    emergency_stop.run_with_emergency_stop(sweep_fn, axes, ["a", "b"], "extra", gui=False)

    assert sweep_calls == [("extra", False)]
    assert all(axis.calls == [("close",)] for axis in axes)


def test_stop_during_sweep_is_reported_as_abort(monkeypatch, capsys):
    monkeypatch.setattr(emergency_stop, "wait_in_console", press_stop_then_start)
    axes = [FakeAxis()]

    def sweep_fn(stop_event):
        if stop_event.is_set():
            raise RuntimeError("Emergency stop")  # what a stage wait raises after a stop

    emergency_stop.run_with_emergency_stop(sweep_fn, axes, ["a"], gui=False)  # must not raise

    assert "Aborted: Emergency stop" in capsys.readouterr().out
    assert axes[0].calls == [("stop",), ("close",)]


def test_unexpected_error_halts_stages_then_closes_and_reraises():
    axes = [FakeAxis(), FakeAxis()]

    def sweep_fn(stop_event):
        raise ConnectionResetError("TOptica went away")

    with pytest.raises(ConnectionResetError):
        emergency_stop.run_with_emergency_stop(sweep_fn, axes, ["a", "b"], gui=False)

    assert all(axis.calls == [("stop",), ("close",)] for axis in axes)


def test_runtime_error_without_a_stop_still_halts_stages(capsys):
    # libximc reports device errors as a bare RuntimeError("General Error"),
    # which used to be swallowed as an "abort" without stopping anything.
    axes = [FakeAxis()]

    def sweep_fn(stop_event):
        raise RuntimeError("General Error")

    emergency_stop.run_with_emergency_stop(sweep_fn, axes, ["a"], gui=False)

    assert axes[0].calls == [("stop",), ("close",)]
    assert "STOP (RuntimeError: General Error)" in capsys.readouterr().out


def test_ctrl_c_triggers_the_stop_and_handler_is_restored_afterwards():
    previous = signal.getsignal(signal.SIGINT)
    axes = [FakeAxis()]
    seen = []

    def sweep_fn(stop_event):
        signal.raise_signal(signal.SIGINT)  # runs the Python-level handler before returning
        seen.append(stop_event.is_set())

    emergency_stop.run_with_emergency_stop(sweep_fn, axes, ["a"], gui=False)

    assert seen == [True]
    assert axes[0].calls == [("stop",), ("close",)]
    assert signal.getsignal(signal.SIGINT) is previous


def test_window_that_cannot_open_never_starts_sweep_but_closes_axes(monkeypatch):
    def no_display(estop, worker, title):
        raise StopWindowUnavailable("no display")

    monkeypatch.setattr(emergency_stop, "run_stop_window", no_display)
    axes = [FakeAxis()]
    ran = []

    with pytest.raises(StopWindowUnavailable):
        emergency_stop.run_with_emergency_stop(lambda stop_event: ran.append(1), axes, ["a"], gui=True)

    assert ran == []
    assert axes[0].calls == [("close",)]


def test_gui_mode_hands_worker_and_title_to_the_stop_window(monkeypatch):
    seen = {}

    def fake_window(estop, worker, title):
        seen["title"] = title
        worker.start()

    monkeypatch.setattr(emergency_stop, "run_stop_window", fake_window)
    ran = []

    emergency_stop.run_with_emergency_stop(
        lambda stop_event: ran.append(1), [FakeAxis()], ["a"], title="Specular sweep"
    )

    assert seen == {"title": "Specular sweep"}
    assert ran == [1]
