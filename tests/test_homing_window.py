"""Drives `reflecto homing` with its real windows; skipped when there is no display."""
import threading

import pytest
from typer.testing import CliRunner

from hi_skuggsja_reflectometry import cli, position_window
from hi_skuggsja_reflectometry.emergency_stop import EmergencyStop
from hi_skuggsja_reflectometry.homing_window import HomingProgress, HomingWindow
from hi_skuggsja_reflectometry.stages import RECEIVER, SAMPLE
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.fakes import FakeAxis


def test_progress_lines_follow_the_homing():
    progress = HomingProgress([RECEIVER, SAMPLE])
    assert progress.lines(stopped=False) == ["Sample R2: homing...", "Receiver R1: waiting"]
    progress.homed(SAMPLE)
    assert progress.lines(stopped=False) == ["Sample R2: homed ✓", "Receiver R1: homing..."]
    assert progress.lines(stopped=True) == ["Sample R2: homed ✓", "Receiver R1: stopped, not homed"]


def test_stop_in_the_homing_window_stops_the_stage():
    axes = [FakeAxis(), FakeAxis()]
    estop = EmergencyStop(axes, ["R1", "R2"])
    progress = HomingProgress([RECEIVER])
    try:
        window = HomingWindow(estop, "Homing Receiver R1", progress, simulated=True)
    except StopWindowUnavailable as exc:
        pytest.skip(f"no display for Tk: {exc}")
    seen = {}

    def press_stop():
        window.button.invoke()
        window._refresh()
        seen["lines"] = window.stage_lines.cget("text")

    window.root.after(200, press_stop)
    window.run(threading.Thread(target=lambda: estop.event.wait(10), daemon=True))

    assert estop.reason == "STOP button"
    assert all(("stop",) in axis.calls for axis in axes)
    assert seen["lines"] == "Receiver R1: stopped, not homed"


def test_homing_command_shows_the_arms_afterwards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shown = {}
    original_run = position_window.PositionWindow.run

    def look_then_close(self):
        def look():
            shown["readout"] = self.readout.cget("text")
            self._cancel()

        self.root.after(500, look)
        return original_run(self)

    monkeypatch.setattr(position_window.PositionWindow, "run", look_then_close)

    result = CliRunner().invoke(cli.app, ["homing", "--simulate"], input="clear\n")
    if "Could not open" in result.output:
        pytest.skip("no display for Tk")

    assert result.exit_code == 0, result.output
    assert "180.50°  homed" in shown["readout"] and "40.00°  homed" in shown["readout"]
