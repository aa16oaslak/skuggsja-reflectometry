"""Drives the real `reflecto check` window; skipped when there is no display."""
import pytest

from hi_skuggsja_reflectometry import hardware
from hi_skuggsja_reflectometry.hardware import FAIL, OK, UNKNOWN, DeviceStatus
from hi_skuggsja_reflectometry.hardware_window import LIGHT, HardwareCheckWindow
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable

RESULT = [
    DeviceStatus("Receiver stage R1", "COM3", OK, "at 60.00°, ready", ["Position 60.00°"]),
    DeviceStatus("TOptica", "10.0.0.5:1998", FAIL, "no answer at 10.0.0.5:1998", ["timed out"]),
]


def make_window(check):
    checking = [DeviceStatus(s.name, s.address, UNKNOWN, "checking...") for s in RESULT]
    try:
        return HardwareCheckWindow(check, checking)
    except StopWindowUnavailable as exc:
        pytest.skip(f"no display for Tk: {exc}")


def rows(window):
    return [
        (light.itemcget(dot, "fill"), title.cget("text"), summary.cget("text"))
        for light, dot, title, summary, _details in window.panel._rows
    ]


def test_shows_each_device_and_returns_the_result_when_closed():
    calls = []

    def check():
        calls.append(1)
        return RESULT

    window = make_window(check)
    seen = {}

    def look_then_check_again_then_close():
        seen["first"] = rows(window)
        window.again.invoke()
        window.root.after(300, window.root.destroy)

    window.root.after(300, look_then_check_again_then_close)
    result = window.run()

    assert result == RESULT
    assert len(calls) == 2  # checked once on opening, once more via "Check again"
    assert seen["first"] == [
        (LIGHT[OK], "Receiver stage R1  (COM3)", "at 60.00°, ready"),
        (LIGHT[FAIL], "TOptica  (10.0.0.5:1998)", "no answer at 10.0.0.5:1998"),
    ]


def test_a_check_that_crashes_is_shown_not_lost():
    def check():
        raise RuntimeError("driver exploded")

    window = make_window(check)
    seen = {}
    window.root.after(300, lambda: (seen.update(footer=window.footer.cget("text")), window.root.destroy()))

    assert window.run() is None
    assert "driver exploded" in seen["footer"]


def test_the_cli_window_starts_from_the_placeholders(cfg):
    placeholders = hardware.unchecked(cfg, simulated=False, summary="checking...")
    assert [(s.name, s.address) for s in placeholders] == [
        ("Receiver stage R1", "COM3"), ("Sample stage R2", "COM4"), ("TOptica", "192.0.2.1:1998"),
    ]


def test_check_command_opens_the_window_and_prints_the_result(monkeypatch):
    from typer.testing import CliRunner

    from hi_skuggsja_reflectometry import cli

    original_run = HardwareCheckWindow.run

    def run_then_close(self):
        self.root.after(1500, self.root.destroy)
        return original_run(self)

    monkeypatch.setattr(cli.HardwareCheckWindow, "run", run_then_close)
    result = CliRunner().invoke(cli.app, ["check", "--simulate"])
    if "Could not open the hardware check window" in result.output:
        pytest.skip("no display for Tk")

    assert result.exit_code == 0, result.output
    assert result.output.count("[ ok ]") == 3
