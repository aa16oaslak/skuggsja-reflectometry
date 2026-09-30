"""Drives the real live window; skipped when there is no display."""
import threading

import pytest

from hi_skuggsja_reflectometry import clock, hardware, live_view, sweeps
from hi_skuggsja_reflectometry.config import ViewConfig
from hi_skuggsja_reflectometry.emergency_stop import EmergencyStop
from hi_skuggsja_reflectometry.hardware import WatchedAxis
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.conftest import FAST


def make_window(rig, plan, simulated=True, verdict=None):
    large, small = WatchedAxis(rig.large), WatchedAxis(rig.small)
    for axis in (large, small):
        axis.get_status()  # as the CLI does, so the view starts where the stages are
    source = live_view.SweepSource(large, small, rig.cfg.stages, hardware.check_simulated(rig), simulated)
    files = [s.output_file(str(rig.output_dir / "run"), 3, 70.0, 70.5) for s in plan]
    estop = EmergencyStop([large, small], ["receiver", "sample"])
    try:
        return live_view.LiveSweepWindow(estop, "Test sweep", source, plan, files, rig.cfg, verdict)
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


def run_sweep_in(window, rig, plan, stop_after_first_scan=False):
    large, small = window._source.large, window._source.small
    stop_event = window._estop.event

    def sweep():
        with clock.accelerated(FAST):
            if stop_after_first_scan:
                real_scan = sweeps.scan

                def scan_then_stop(*args):
                    real_scan(*args)
                    window._estop.trigger("STOP button")

                sweeps.scan = scan_then_stop
            try:
                sweeps.sweep_spec(
                    large, small, rig.cfg, plan[0].sample, plan[-1].sample, 7.5, 70.0, 70.5, 3,
                    str(rig.output_dir / "run"), False, True, stop_event,
                )
            finally:
                if stop_after_first_scan:
                    sweeps.scan = real_scan

    return threading.Thread(target=sweep, daemon=True)


def click_when_asked(window, button_name, seen):
    def click():
        button = getattr(window, button_name, None)
        if button is None:  # not asked yet
            window.root.after(50, click)
            return
        seen["verdict"] = window.verdict_text.cget("text")
        seen["run_button"] = window.run_button is not None
        button.invoke()

    window.root.after(50, click)


def test_simulation_before_a_real_sweep_asks_to_run_it_when_it_went_well(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    window = make_window(rig, plan, verdict=lambda: rig.verdict(plan))
    seen = {}
    click_when_asked(window, "run_button", seen)

    window.run(run_sweep_in(window, rig, plan))

    assert window.decision is True
    assert "All 3 steps were scanned at the planned angles" in seen["verdict"]


def test_simulation_before_a_real_sweep_can_be_cancelled(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    window = make_window(rig, plan, verdict=lambda: rig.verdict(plan))
    seen = {}
    click_when_asked(window, "cancel_button", seen)

    window.run(run_sweep_in(window, rig, plan))

    assert window.decision is False


def test_a_stopped_simulation_offers_no_way_to_run_on_hardware(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    window = make_window(rig, plan, verdict=lambda: rig.verdict(plan))
    seen = {}
    click_when_asked(window, "cancel_button", seen)

    window.run(run_sweep_in(window, rig, plan, stop_after_first_scan=True))

    assert window.decision is False
    assert seen["run_button"] is False
    assert "did not finish (STOP button)" in seen["verdict"]
    assert "Only 1 of 3 steps were reached" in seen["verdict"]


@pytest.mark.parametrize(
    "turns, tx_direction, tx_at, receiver_at",
    [
        # canvas y grows downwards; a receiver at 90° from Tx
        ("clockwise", 0, (370, 270), (270, 370)),  # the paper's Fig. 1: Tx right, Rx below
        ("counterclockwise", 0, (370, 270), (270, 170)),  # mirrored: Rx above
        ("clockwise", 180, (170, 270), (270, 170)),  # turned half a turn: Tx left, Rx above
        ("clockwise", 90, (270, 170), (370, 270)),  # Tx up, Rx right
    ],
)
def test_drawing_turns_and_mirrors_as_the_view_says(turns, tx_direction, tx_at, receiver_at):
    view = ViewConfig(receiver_turns=turns, tx_direction=tx_direction)
    assert live_view.screen_point(0, 100, 270, view) == pytest.approx(tx_at)
    assert live_view.screen_point(90, 100, 270, view) == pytest.approx(receiver_at)


@pytest.mark.parametrize("turns, tx_direction", [("clockwise", 0), ("counterclockwise", 0), ("clockwise", 135)])
def test_arcs_run_between_the_same_points_as_the_drawing(turns, tx_direction):
    # Tk's arcs: start counterclockwise from the right, extent counterclockwise
    # (negative: clockwise). Both ends must land where screen_point puts them.
    import math

    view = ViewConfig(receiver_turns=turns, tx_direction=tx_direction)
    start, extent = live_view.arc_angles(30, 180, view)
    for tk_angle, angle in ((start, 30), (start + extent, 180)):
        a = math.radians(tk_angle)
        assert (270 + 100 * math.cos(a), 270 - 100 * math.sin(a)) == pytest.approx(
            live_view.screen_point(angle, 100, 270, view)
        )
    assert abs(extent) == 150  # the short way through the receiver's range, not the other 210°
