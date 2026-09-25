"""Drives the real live window; skipped when there is no display."""
import threading

import pytest

from hi_skuggsja_reflectometry import clock, live_view, sweeps
from hi_skuggsja_reflectometry.emergency_stop import EmergencyStop
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.conftest import FAST


def make_window(rig, plan):
    files = [s.output_file(str(rig.output_dir / "run"), 3, 70.0, 70.5) for s in plan]
    estop = EmergencyStop([rig.large, rig.small], ["receiver", "sample"])
    try:
        return live_view.LiveSweepWindow(estop, "Test sweep", rig, plan, files, 11, rig.cfg.stages)
    except StopWindowUnavailable as exc:
        pytest.skip(f"no display for Tk: {exc}")


def test_panel_shows_live_angles_against_the_plan(rig):
    window = make_window(rig, sweeps.plan_spec(15.0, 30.0, 7.5))
    try:
        text = window.details.cget("text")
        assert "Step         1 of 3" in text
        assert "plan   15.00°" in text and "plan   30.00°" in text
        assert window.warning.cget("text") == ""
    finally:
        window.root.destroy()


def test_warns_when_the_receiver_is_outside_its_soft_limits(rig):
    window = make_window(rig, sweeps.plan_spec(15.0, 30.0, 7.5))
    try:
        rig.large.command_homezero()  # the receiver's home, 180.5°, is past the 180° limit
        window._refresh()
        assert "outside the soft limits" in window.warning.cget("text")
    finally:
        window.root.destroy()


def test_warns_about_planned_angles_outside_the_soft_limits(rig):
    window = make_window(rig, sweeps.plan_spec(80.0, 95.0, 7.5))  # receiver up to 190°
    try:
        assert "planned receiver angles are outside" in window.warning.cget("text")
    finally:
        window.root.destroy()


def test_follows_a_whole_simulated_sweep_and_closes_at_the_end(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    window = make_window(rig, plan)
    stop_event = window._estop.event

    def sweep():
        with clock.accelerated(FAST):
            sweeps.sweep_spec(
                rig.large, rig.small, rig.cfg, 15.0, 30.0, 7.5, 70.0, 70.5, 3,
                str(rig.output_dir / "run"), False, True, stop_event,
            )

    window.run(threading.Thread(target=sweep, daemon=True))  # returns when the window closes

    assert len(rig.toptica.scans) == len(plan)
