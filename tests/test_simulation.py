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


def test_nonspec_with_set_zero_leaves_sample_at_home_not_15(rig, monkeypatch):
    # A known bug the simulation makes visible: after --set-zero the sample is
    # never moved to NONSPEC_SAMPLE_ANGLE, so the scan runs at the sample's home.
    monkeypatch.setattr("builtins.input", lambda _prompt: "clear")
    with clock.accelerated(FAST):
        sweeps.sweep_nonspec(
            rig.large, rig.small, rig.cfg, 45.0, 45.0, 15.0, 70.0, 70.2, 3,
            str(rig.output_dir / "run"), True, True, threading.Event(),
        )
    assert rig.toptica.scans[0].sample == pytest.approx(-rig.cfg.stages.zero_s)  # 40°, not 15°
