import threading

import pytest

from hi_skuggsja_reflectometry import clock, hardware
from hi_skuggsja_reflectometry.arm_mover import ArmMover
from hi_skuggsja_reflectometry.stages import RECEIVER, SAMPLE
from tests.conftest import FAST
from tests.fakes import FakeAxis


def wait_done(mover):
    while mover.state()[0] is not None:
        clock.sleep(0.05)


def reading(axis):
    return hardware.read_arm(axis)


def angles(rig):
    return tuple(round(a, 6) for a in rig.angles())  # (sample, receiver)


@pytest.mark.parametrize("stage, delta", [(RECEIVER, 2.0), (RECEIVER, -1.5), (SAMPLE, -3.0), (SAMPLE, 0.25)])
def test_moves_an_arm_by_that_many_degrees_of_its_angle(rig, stage, delta):
    before = rig.angles()
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    axis = rig.large if stage == RECEIVER else rig.small
    with clock.accelerated(FAST):
        assert mover.move(stage, delta, reading(axis)) is None
        wait_done(mover)

    sample, receiver = rig.angles()
    if stage == RECEIVER:
        assert (receiver - before[1], sample) == (pytest.approx(delta), pytest.approx(before[0]))
    else:
        assert (sample - before[0], receiver) == (pytest.approx(delta), pytest.approx(before[1]))
    busy, message, ok = mover.state()
    assert busy is None and ok and f"moved {delta:+g}°" in message


@pytest.mark.parametrize("delta", [0, 10.5, -11])
def test_refuses_big_or_empty_steps(rig, delta):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    assert "at most 10°" in mover.move(RECEIVER, delta, reading(rig.large))
    assert rig.large.moves == 0


def test_refuses_a_stage_it_cannot_read_or_that_is_moving(rig):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    assert "can't be read" in mover.move(SAMPLE, 1, None)
    moving = hardware.ArmReading(0.0, homed=True, moving=True)
    assert "is moving" in mover.move(SAMPLE, 1, moving)
    assert rig.small.moves == 0


def test_soft_limits_are_checked_once_the_receiver_is_homed(rig):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    at_179 = hardware.ArmReading(rig.zero_l - 179, homed=True, moving=False)
    assert "outside its soft limits 30°-180°" in mover.move(RECEIVER, 2, at_179)
    assert rig.large.moves == 0

    # not homed: the limits are counted from an unknown zero, so they don't stop a small move
    not_homed = hardware.ArmReading(rig.zero_l - 179, homed=False, moving=False)
    with clock.accelerated(FAST):
        assert mover.move(RECEIVER, 2, not_homed) is None
        wait_done(mover)


def test_one_move_at_a_time(rig):
    rig.large.speed = 0.5  # slow, so the first move is still going
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    with clock.accelerated(1):  # real time: the rig fixture otherwise runs the clock fast
        assert mover.move(RECEIVER, 5, reading(rig.large)) is None
        assert "Wait until the current move has finished" in mover.move(SAMPLE, 1, reading(rig.small))
        mover.close()


def test_stop_halts_a_move_midway(rig):
    rig.large.speed = 0.5  # 10° takes 20 s
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    start = rig.angles()[1]
    with clock.accelerated(1):  # real time: the rig fixture otherwise runs the clock fast
        assert mover.move(RECEIVER, 10, reading(rig.large)) is None
        threading.Event().wait(0.3)
        mover.stop()
        threading.Event().wait(0.3)

    busy, message, ok = mover.state()
    assert busy is None and not ok and "stopped" in message
    assert not rig.large.moving
    assert 0 < rig.angles()[1] - start < 10


def test_a_move_the_controller_ends_at_a_soft_limit_is_reported(rig):
    rig.large.homed = False
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    with clock.accelerated(FAST):
        rig.large.command_move_calb(rig.zero_l - 31)  # receiver at 31°
        while rig.large.moving:
            clock.sleep(0.05)
        assert mover.move(RECEIVER, -2, reading(rig.large)) is None  # 29°: past the controller's 30° limit
        wait_done(mover)

    _busy, message, ok = mover.state()
    assert not ok and "ended the move early" in message
    assert rig.angles()[1] == pytest.approx(30, abs=0.01)


def test_close_stops_the_stages_only_if_it_moved_them():
    idle = ArmMover(FakeAxis(), FakeAxis(), None)
    idle.close()
    assert all(call[0] != "stop" for axis in idle._axes.values() for call in axis.calls)


def test_close_stops_a_move_in_progress(rig):
    rig.large.speed = 0.5
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    with clock.accelerated(1):  # real time: the rig fixture otherwise runs the clock fast
        assert mover.move(RECEIVER, 5, reading(rig.large)) is None
        threading.Event().wait(0.2)
        assert rig.large.moving

        mover.close()

        assert not rig.large.moving and mover.state()[0] is None
