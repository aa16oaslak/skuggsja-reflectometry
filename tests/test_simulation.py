import threading
import time

import pytest

from hi_skuggsja_reflectometry import clock, simulation, stages, sweeps, toptica
from hi_skuggsja_reflectometry import config as cfgmod
from tests.conftest import FAST


def wait_until_stopped(axis):
    while axis.moving:
        clock.sleep(0.05)


# -- clock ------------------------------------------------------------------------


def test_accelerated_clock_shortens_waits_but_counts_them_in_full():
    with clock.accelerated(100):
        t_clock, t_real = clock.time(), time.monotonic()
        clock.sleep(2.0)
        assert clock.time() - t_clock >= 2.0
        assert time.monotonic() - t_real < 0.5


def test_clock_at_normal_speed_keeps_pace_with_the_wall_clock():
    assert clock.speed() == 1.0
    ahead = clock.time() - time.time()  # a simulation earlier in the process may have moved it ahead
    clock.sleep(0.01)
    assert clock.time() - time.time() == pytest.approx(ahead, abs=0.005)
    with pytest.raises(ValueError):
        clock.set_speed(0)


# -- simulated stage ------------------------------------------------------------------


def test_sim_axis_move_takes_distance_over_speed():
    with clock.accelerated(FAST):
        axis = simulation.SimAxis("test", speed=10.0, position=0.0)
        axis.open_device()
        t0 = clock.time()
        axis.command_move_calb(5.0)
        assert axis.moving
        assert int(axis.get_status().MvCmdSts) & stages.MVCMD_RUNNING
        wait_until_stopped(axis)
        assert axis.get_position_calb().Position == 5.0
        assert clock.time() - t0 == pytest.approx(0.5, abs=0.06)


def test_sim_axis_relative_move_while_moving_shifts_the_target():
    with clock.accelerated(FAST):
        axis = simulation.SimAxis("test", speed=10.0, position=0.0)
        axis.open_device()
        axis.command_move_calb(5.0)
        axis.command_movr_calb(2.0)
        wait_until_stopped(axis)
        assert axis.position == 7.0


def test_sim_axis_stops_at_soft_limit_and_flags_it(cfg):
    with clock.accelerated(FAST):
        axis = simulation.SimAxis("receiver", speed=50.0, position=cfg.stages.zero_l - 90)
        axis.open_device()
        axis.set_calb(cfg.stages.res_large, 9)
        stages.set_boundaries(axis, cfg.stages.res_large, 30, 180, cfg.stages.zero_l)

        axis.command_move_calb(cfg.stages.zero_l - 20)  # receiver angle 20°, below the 30° limit
        assert not int(axis.get_status().MvCmdSts) & stages.MVCMD_ERROR  # not flagged while still on its way
        wait_until_stopped(axis)

        assert int(axis.get_status().MvCmdSts) & stages.MVCMD_ERROR
        assert cfg.stages.zero_l - axis.position == pytest.approx(30, abs=0.01)
        assert len(axis.border_hits) == 1


def test_sim_axis_stop_halts_mid_move():
    with clock.accelerated(FAST):
        axis = simulation.SimAxis("test", speed=10.0, position=0.0)
        axis.open_device()
        axis.command_move_calb(10.0)
        clock.sleep(0.3)
        axis.command_stop()
        assert not axis.moving
        assert 1.0 < axis.position < 9.0


def test_sim_axis_homing_blocks_until_home_and_can_be_interrupted():
    with clock.accelerated(FAST):
        axis = simulation.SimAxis("test", speed=10.0, position=20.0)
        axis.open_device()
        axis.command_homezero()
        assert axis.position == 0.0

        axis.command_move_calb(20.0)
        wait_until_stopped(axis)
        threading.Timer(0.001, axis.command_stop).start()  # STOP pressed during homing
        axis.command_homezero()
        assert axis.position > 0.0  # stopped short of home


def test_sim_axis_refuses_calls_when_closed():
    axis = simulation.SimAxis("test", speed=10.0, position=0.0)
    with pytest.raises(RuntimeError, match="not open"):
        axis.command_move_calb(1.0)


# -- simulated TOptica with the real scan() -------------------------------------------


def test_real_scan_runs_against_simulated_toptica(cfg, tmp_path):
    angles = {"now": (20.0, 40.0)}  # specular
    sim = simulation.SimToptica(cfg.toptica, lambda: angles["now"])
    tcfg = cfgmod.TopticaConfig(**{**vars(cfg.toptica), "host": "127.0.0.1", "port": sim.port})
    try:
        with clock.accelerated(FAST):
            toptica.scan(70, 71, 3, str(tmp_path / "spec"), threading.Event(), tcfg)
            angles["now"] = (20.0, 60.0)  # far from specular
            toptica.scan(70, 71, 3, str(tmp_path / "off"), threading.Event(), tcfg)
    finally:
        sim.close()

    import numpy as np

    spec = np.loadtxt(tmp_path / "spec_3ms_70GHz_to_71.txt")
    off = np.loadtxt(tmp_path / "off_3ms_70GHz_to_71.txt")
    assert len(spec) == len(toptica.frequency_grid(70, 71, cfg.toptica.freq_step))
    assert np.abs(spec[:, 3]).max() > 10 * np.abs(off[:, 3]).max()
    assert [s.points for s in sim.scans] == [len(spec), len(off)]


# -- sweeps on the simulated rig ------------------------------------------------------------


@pytest.mark.parametrize(
    "sweep_fn, plan_fn, start, end, step",
    [
        (sweeps.sweep_spec, sweeps.plan_spec, 15.0, 30.0, 7.5),
        (sweeps.sweep_nonspec, sweeps.plan_nonspec, 30.0, 60.0, 15.0),
    ],
)
def test_sweeps_scan_at_exactly_the_planned_angles(rig, sweep_fn, plan_fn, start, end, step):
    with clock.accelerated(FAST):
        sweep_fn(
            rig.large, rig.small, rig.cfg, start, end, step, 70.0, 70.5, 3,
            str(rig.output_dir / "run"), False, True, threading.Event(),
        )
    plan = plan_fn(start, end, step)
    assert len(rig.toptica.scans) == len(plan)
    for scan, planned in zip(rig.toptica.scans, plan):
        # 0.01°: parking at the 30° limit stops ~0.006° short, and relative
        # moves carry that on (see the summary's soft-limit note)
        assert scan.sample == pytest.approx(planned.sample, abs=0.01)
        assert scan.receiver == pytest.approx(planned.receiver, abs=0.01)


def test_summary_lists_steps_and_soft_limit_stops(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    prefix = str(rig.output_dir / "run")
    with clock.accelerated(FAST):
        sweeps.sweep_spec(
            rig.large, rig.small, rig.cfg, 15.0, 22.5, 7.5, 70.0, 70.5, 3, prefix, False, True, threading.Event(),
        )
    files = [rig.output_dir / f"run_{s.file_angle}degrees_3ms_70.0GHz_to_70.5.txt" for s in plan]

    text = rig.summary(plan, files, real_seconds=1.0)

    assert "no hardware was used" in text
    assert text.count(" ok ") == 2
    assert "3/3" in text and "not reached" in text  # the sweep above stopped at 22.5°
    assert "stopped at the soft limit" in text


def test_nonspec_with_set_zero_scans_with_the_sample_at_15_not_at_home(rig):
    # used to be a bug: after --set-zero the sample stayed at its home, 40°
    with clock.accelerated(FAST):
        sweeps.sweep_nonspec(
            rig.large, rig.small, rig.cfg, 45.0, 45.0, 15.0, 70.0, 70.2, 3,
            str(rig.output_dir / "run"), True, True, threading.Event(),
        )
    assert rig.toptica.scans[0].sample == pytest.approx(sweeps.NONSPEC_SAMPLE_ANGLE)
    assert rig.toptica.scans[0].receiver == pytest.approx(45.0)


def test_homing_a_simulated_stage_and_stopping_it_midway(rig):
    rig.large.homed = False
    with clock.accelerated(FAST):
        stages.home_stage(rig.large, stages.RECEIVER, rig.cfg.stages, threading.Event())
        assert (rig.large.position, rig.large.homed) == (0.0, True)

        rig.large.command_move_calb(rig.zero_l - 90)
        wait_until_stopped(rig.large)
        rig.large.speed = 1.0  # the way home now takes 90 s of clock time: ~90 ms real, so the stop lands midway
        stop_event = threading.Event()
        threading.Timer(0.001, stop_event.set).start()  # STOP soon after homing starts
        with pytest.raises(RuntimeError, match="not homed"):
            stages.home_stage(rig.large, stages.RECEIVER, rig.cfg.stages, stop_event)

    assert rig.large.homed is False
    assert rig.large.position > 1.0  # stopped on the way, and its count not zeroed


def test_simulation_can_start_where_the_real_arms_are(cfg):
    from hi_skuggsja_reflectometry.hardware import ArmReading

    start = (ArmReading(2.4, homed=False, moving=False, speed=12.0), ArmReading(188.73, True, False, None))
    with clock.accelerated(FAST):
        rig = simulation.SimRig(cfg, start=start)
    try:
        assert (rig.large.position, rig.small.position) == (2.4, 188.73)
        assert (rig.large.homed, rig.small.homed) == (False, True)
        assert (rig.large.speed, rig.small.speed) == (12.0, simulation.SAMPLE_SPEED)  # guessed where not read
        assert rig.speeds_read is False
        assert rig.angles() == pytest.approx((228.73, 178.1))
    finally:
        rig.close()


def test_verdict_after_a_full_and_a_cut_short_sweep(rig):
    plan = sweeps.plan_spec(15.0, 30.0, 7.5)
    with clock.accelerated(FAST):
        sweeps.sweep_spec(
            rig.large, rig.small, rig.cfg, 15.0, 22.5, 7.5, 70.0, 70.5, 3,
            str(rig.output_dir / "run"), False, True, threading.Event(),
        )

    ok, lines = rig.verdict(plan[:2])
    assert ok and lines[0] == "All 2 steps were scanned at the planned angles."
    ok, lines = rig.verdict(plan)
    assert not ok and "Only 2 of 3 steps were reached." in lines
