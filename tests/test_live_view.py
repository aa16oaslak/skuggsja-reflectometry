"""Drives the real live window; skipped when there is no display."""
import threading

import pytest

from hi_skuggsja_reflectometry import clock, hardware, live_view, sweeps
from hi_skuggsja_reflectometry.emergency_stop import EmergencyStop
from hi_skuggsja_reflectometry.hardware import WatchedAxis
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.conftest import FAST


def make_window(rig, plan, simulated=True):
    large, small = WatchedAxis(rig.large), WatchedAxis(rig.small)
    for axis in (large, small):
        axis.get_status()  # as the CLI does, so the view starts where the stages are
    source = live_view.SweepSource(large, small, rig.cfg.stages, hardware.check_simulated(rig), simulated)
    files = [s.output_file(str(rig.output_dir / "run"), 3, 70.0, 70.5) for s in plan]
    estop = EmergencyStop([large, small], ["receiver", "sample"])
    try:
        return live_view.LiveSweepWindow(estop, "Test sweep", source, plan, files, rig.cfg.stages)
    except StopWindowUnavailable as exc:
        pytest.skip(f"no display for Tk: {exc}")


def test_readouts_show_live_angles_against_the_plan(rig):
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
        window._source.large.command_homezero()  # the receiver's home, 180.5°, is past the 180° limit
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


def test_hardware_lights_turn_red_when_a_stage_stops_answering(rig):
    window = make_window(rig, sweeps.plan_spec(15.0, 30.0, 7.5))
    try:
        rig.large.close_device()  # as if the cable were pulled
        with pytest.raises(RuntimeError):
            window._source.large.get_status()
        window._refresh()
        light, dot, _title, summary, _details = window.hardware._rows[0]
        assert light.itemcget(dot, "fill") == "#c62828"
        assert "stopped answering" in summary.cget("text")
    finally:
        window.root.destroy()


def test_real_sweeps_are_labelled_as_real(rig):
    window = make_window(rig, sweeps.plan_spec(15.0, 30.0, 7.5), simulated=False)
    try:
        assert window.root.title() == "reflecto - sweep (real hardware)"
    finally:
        window.root.destroy()


def test_follows_a_whole_simulated_sweep_and_closes_at_the_end(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    window = make_window(rig, plan)
    large, small = window._source.large, window._source.small
    stop_event = window._estop.event

    def sweep():
        with clock.accelerated(FAST):
            sweeps.sweep_spec(
                large, small, rig.cfg, 15.0, 30.0, 7.5, 70.0, 70.5, 3,
                str(rig.output_dir / "run"), False, True, stop_event,
            )

    window.run(threading.Thread(target=sweep, daemon=True))  # returns when the window closes

    assert len(rig.toptica.scans) == len(plan)
    assert window._source.scan().started == len(plan)
