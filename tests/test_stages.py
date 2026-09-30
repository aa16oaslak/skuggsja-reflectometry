import sys
import threading

import libximc.highlevel as real_ximc
import pytest

from hi_skuggsja_reflectometry import stages
from tests.fakes import FakeAxis

ZERO_L = 180.5
ZERO_S = -40
ANGLE_MIN = 30
ANGLE_MAX = 180


def test_degrees_to_microsteps():
    assert stages.degrees_to_microsteps(0.0072, 180.5) == int(180.5 / 0.0072)
    assert stages.degrees_to_microsteps(0.015, -40) == int(-40 / 0.015)


def test_set_speed_accel_applies_values():
    axis = FakeAxis()
    stages.set_speed_accel(axis, 8, 5)
    assert axis.move_settings.Speed == 8
    assert axis.move_settings.Accel == 5


@pytest.mark.parametrize("speed,accel", [(0, 5), (11, 5), (5, 0), (5, 11)])
def test_set_speed_accel_rejects_out_of_range(speed, accel):
    axis = FakeAxis()
    with pytest.raises(ValueError):
        stages.set_speed_accel(axis, speed, accel)


def test_rotate_to_angle_large_moves_and_negates():
    axis = FakeAxis()
    stop_event = threading.Event()

    stages.rotate_to_angle(axis, 60, True, stop_event, ZERO_L, ZERO_S, ANGLE_MIN, ANGLE_MAX)

    assert axis.position == pytest.approx(ZERO_L - 60)
    assert ("move_calb", ZERO_L - 60) in axis.calls


def test_rotate_to_angle_large_out_of_bounds_raises_before_moving():
    axis = FakeAxis()
    stop_event = threading.Event()

    with pytest.raises(ValueError):
        stages.rotate_to_angle(axis, 200, True, stop_event, ZERO_L, ZERO_S, ANGLE_MIN, ANGLE_MAX)

    assert axis.calls == []  # never issued a move command


def test_rotate_to_angle_small_uses_zero_s_and_no_bounds_check():
    axis = FakeAxis()
    stop_event = threading.Event()

    stages.rotate_to_angle(axis, 15, False, stop_event, ZERO_L, ZERO_S, ANGLE_MIN, ANGLE_MAX)

    assert axis.position == pytest.approx(ZERO_S + 15)


def test_rotate_relative_large_within_bounds():
    axis = FakeAxis(position=ZERO_L - 60)  # currently parked at 60 degrees
    stop_event = threading.Event()

    stages.rotate_relative(axis, 15, True, stop_event, ZERO_L, ANGLE_MIN, ANGLE_MAX)

    assert axis.position == pytest.approx(ZERO_L - 75)  # moved to 75 degrees


def test_rotate_relative_large_out_of_bounds_raises_before_moving():
    axis = FakeAxis(position=ZERO_L - 170)  # currently parked at 170 degrees
    stop_event = threading.Event()

    with pytest.raises(ValueError):
        stages.rotate_relative(axis, 20, True, stop_event, ZERO_L, ANGLE_MIN, ANGLE_MAX)

    assert axis.calls == []


def test_rotate_relative_small_has_no_bounds_check():
    axis = FakeAxis(position=0)
    stop_event = threading.Event()

    stages.rotate_relative(axis, 500, False, stop_event, ZERO_L, ANGLE_MIN, ANGLE_MAX)

    assert axis.position == 500


def test_wait_for_stop_returns_when_axis_reports_stopped():
    axis = FakeAxis()
    stop_event = threading.Event()

    stages.wait_for_stop(axis, stop_event)  # should not raise or hang


def test_wait_for_stop_raises_and_stops_axis_if_stop_event_already_set():
    axis = FakeAxis()
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(RuntimeError):
        stages.wait_for_stop(axis, stop_event)

    assert ("stop",) in axis.calls


def test_position_large_uses_zero_l():
    axis = FakeAxis(position=120)  # already an int, avoids int()-truncation ambiguity
    assert stages.position(axis, True, ZERO_L, ZERO_S) == -120 + ZERO_L


def test_position_small_uses_zero_s():
    axis = FakeAxis(position=-25)
    # regression check for the zero_s sign bugfix: Position - zero_s, not Position + zero_s
    assert stages.position(axis, False, ZERO_L, ZERO_S) == 15


def test_set_boundaries_computes_edges_from_zero_l():
    axis = FakeAxis()
    stages.set_boundaries(axis, 0.0072, ANGLE_MIN, ANGLE_MAX, ZERO_L)

    assert axis.edges.LeftBorder == stages.degrees_to_microsteps(0.0072, ZERO_L - ANGLE_MAX)
    assert axis.edges.RightBorder == stages.degrees_to_microsteps(0.0072, ZERO_L - ANGLE_MIN)


def stage_cfg():
    from hi_skuggsja_reflectometry.config import StageConfig

    return StageConfig(
        device_uri_large="COM3", device_uri_small="COM4", zero_l=ZERO_L, zero_s=ZERO_S,
        res_large=0.0072, res_small=0.015, angle_min=ANGLE_MIN, angle_max=ANGLE_MAX,
    )


class RecordingAxis(FakeAxis):
    """Notes whether stopping at the soft limits was on when homing started."""

    def command_home(self):
        self.border_flags_while_homing = int(self.edges.BorderFlags)
        super().command_home()


def test_home_stage_homes_with_limits_off_then_zeroes_and_sets_the_limits():
    axis = RecordingAxis(position=150.49)
    axis.edges.BorderFlags = 0x07  # stop at both soft limits, as a sweep leaves it

    stages.home_stage(axis, stages.RECEIVER, stage_cfg(), threading.Event())

    kinds = [call[0] for call in axis.calls]
    assert kinds.index("home") < kinds.index("zero")
    assert axis.border_flags_while_homing & (stages.BORDER_STOP_LEFT | stages.BORDER_STOP_RIGHT) == 0
    assert int(axis.edges.BorderFlags) == 0x07  # the limits are back on, now counted from home
    assert axis.edges.LeftBorder == stages.degrees_to_microsteps(0.0072, ZERO_L - ANGLE_MAX)


def test_home_stage_leaves_the_sample_limits_alone():
    axis = FakeAxis(position=17.59)
    before = axis.edges

    stages.home_stage(axis, stages.SAMPLE, stage_cfg(), threading.Event())

    assert [call[0] for call in axis.calls] == ["home", "zero"]
    assert axis.edges is before


def test_home_stage_does_nothing_if_already_stopped():
    axis = FakeAxis()
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(RuntimeError, match="emergency stop before homing"):
        stages.home_stage(axis, stages.RECEIVER, stage_cfg(), stop_event)

    assert axis.calls == []


def test_home_stage_stopped_midway_is_not_zeroed_and_the_limits_are_restored():
    stop_event = threading.Event()

    class StoppedWhileHoming(FakeAxis):
        def command_home(self):
            super().command_home()
            stop_event.set()  # STOP pressed while it travels

    axis = StoppedWhileHoming(position=150.49)
    axis.edges.BorderFlags = 0x07

    with pytest.raises(RuntimeError, match="stopped before it finished; not homed"):
        stages.home_stage(axis, stages.RECEIVER, stage_cfg(), stop_event)

    assert ("zero",) not in axis.calls and ("stop",) in axis.calls
    assert int(axis.edges.BorderFlags) == 0x07


def test_home_stage_does_not_zero_if_the_controller_does_not_report_homed():
    class NeverHomed(FakeAxis):
        def get_status(self):
            status = super().get_status()
            status.Flags = 0
            return status

    axis = NeverHomed(position=150.49)
    axis.edges.BorderFlags = 0x07

    with pytest.raises(RuntimeError, match="without the controller reporting the stage homed") as failure:
        stages.home_stage(axis, stages.RECEIVER, stage_cfg(), threading.Event())

    assert ("zero",) not in axis.calls
    assert int(axis.edges.BorderFlags) == 0x07
    assert "last command home" in str(failure.value)
    assert "until its revolution sensor" in str(failure.value)


def test_a_homing_that_does_not_move_says_so():
    class EndsAtOnce(FakeAxis):
        def command_home(self):  # the controller takes the command but ends it with an error at once
            self.calls.append(("home",))
            self.command = 0x06 | 0x40

    axis = EndsAtOnce(position=108.57)

    with pytest.raises(RuntimeError) as failure:
        stages.home_stage(axis, stages.SAMPLE, stage_cfg(), threading.Event())

    message = str(failure.value)
    assert "it did not move at all" in message and "ended with an error" in message
    assert ("zero",) not in axis.calls


def test_a_controller_that_does_not_take_up_the_home_command_is_stopped(monkeypatch):
    monkeypatch.setattr(stages, "HOME_START_TIMEOUT", 0.05)

    class Deaf(FakeAxis):
        def command_home(self):
            self.calls.append(("home",))  # the move status still names the last move

    axis = Deaf(position=108.57)
    axis.command = 0x02

    with pytest.raises(RuntimeError, match="did not take up the home command \\(last command relative move"):
        stages.home_stage(axis, stages.SAMPLE, stage_cfg(), threading.Event())

    assert ("stop",) in axis.calls and ("zero",) not in axis.calls


def test_soft_limit_stops_on_the_sample_are_off_while_it_homes_and_back_after():
    axis = RecordingAxis(position=108.57)
    axis.edges.BorderFlags = 0x07  # e.g. left there when the ports were swapped

    stages.home_stage(axis, stages.SAMPLE, stage_cfg(), threading.Event())

    assert axis.border_flags_while_homing & (stages.BORDER_STOP_LEFT | stages.BORDER_STOP_RIGHT) == 0
    assert int(axis.edges.BorderFlags) == 0x07


@pytest.mark.parametrize(
    "flags, text",
    [
        (0x011, "first towards increasing counts until its revolution sensor"),
        (0x030, "first towards decreasing counts until a limit switch"),
        (0x000, "until nothing (no stop condition set)"),
        (0x0A7, "then towards increasing counts until its sync input"),
    ],
)
def test_describe_homing(flags, text):
    from types import SimpleNamespace

    axis = FakeAxis()
    axis.get_home_settings = lambda: SimpleNamespace(HomeFlags=flags, FastHome=400, HomeDelta=0)
    assert text in stages.describe_homing(axis)


def test_describe_move_status():
    assert stages.describe_move_status(0x46) == "last command home, ended with an error (MvCmdSts 0x46)"
    assert stages.describe_move_status(0x81) == "last command move, running (MvCmdSts 0x81)"


@pytest.mark.parametrize(
    "which, order",
    [
        ((stages.RECEIVER, stages.SAMPLE), [stages.SAMPLE, stages.RECEIVER]),  # the sample always first
        ((stages.RECEIVER,), [stages.RECEIVER]),
        ((stages.SAMPLE,), [stages.SAMPLE]),
    ],
)
def test_home_stages_homes_only_what_was_asked_sample_first(which, order):
    large, small = FakeAxis(position=150.49), FakeAxis(position=17.59)
    homed = []

    stages.home_stages(large, small, which, stage_cfg(), homed.append, threading.Event())

    assert homed == order
    assert (("home",) in large.calls) == (stages.RECEIVER in which)
    assert (("home",) in small.calls) == (stages.SAMPLE in which)


def test_homing_direction_is_read_from_the_home_settings():
    axis = FakeAxis()
    assert stages.homing_direction(axis) == 1

    axis.get_home_settings = lambda: (_ for _ in ()).throw(RuntimeError("no"))
    assert stages.homing_direction(axis) is None


def test_open_stages_calibrates_and_sets_boundaries(monkeypatch):
    fake_large, fake_small = FakeAxis(), FakeAxis()
    created = []

    def fake_ctor(uri):
        axis = fake_large if not created else fake_small
        created.append(uri)
        return axis

    # stages._load_ximc() does `import libximc.highlevel as ximc` at call
    # time and returns the cached module object, so patching the real
    # module's Axis is what actually takes effect.
    monkeypatch.setattr(real_ximc, "Axis", fake_ctor)

    from hi_skuggsja_reflectometry.config import StageConfig

    cfg = StageConfig(
        device_uri_large="COM3",
        device_uri_small="COM4",
        zero_l=ZERO_L,
        zero_s=ZERO_S,
        res_large=0.0072,
        res_small=0.015,
        angle_min=ANGLE_MIN,
        angle_max=ANGLE_MAX,
    )

    large_stage, small_stage = stages.open_stages(cfg)

    assert large_stage is fake_large
    assert small_stage is fake_small
    assert ("open",) in large_stage.calls
    assert ("open",) in small_stage.calls
    assert large_stage.calibration == (0.0072, 9)
    assert small_stage.calibration == (0.015, 9)
    # boundaries were applied to the large stage using cfg's res/angle bounds
    assert large_stage.edges.LeftBorder == stages.degrees_to_microsteps(0.0072, ZERO_L - ANGLE_MAX)


def test_load_ximc_reports_missing_native_library_cleanly(monkeypatch):
    # Setting a sys.modules entry to None forces the next `import` of that
    # name to raise ImportError, simulating the native .so/.dll failing to
    # load without needing to actually break the installed library.
    monkeypatch.setitem(sys.modules, "libximc.highlevel", None)

    with pytest.raises(stages.HardwareUnavailable, match="Could not load the Standa libximc native library"):
        stages._load_ximc()


def test_open_stages_reports_device_open_failure_cleanly(monkeypatch):
    def failing_ctor(uri):
        class FailingAxis(FakeAxis):
            def open_device(self):
                raise ConnectionError(f"Cannot connect to device via URI='{uri}'")
        return FailingAxis()

    monkeypatch.setattr(real_ximc, "Axis", failing_ctor)

    from hi_skuggsja_reflectometry.config import StageConfig

    cfg = StageConfig(
        device_uri_large="xi-com:\\\\.\\COM99",
        device_uri_small="xi-com:\\\\.\\COM98",
        zero_l=ZERO_L, zero_s=ZERO_S,
        res_large=0.0072, res_small=0.015,
        angle_min=ANGLE_MIN, angle_max=ANGLE_MAX,
    )

    with pytest.raises(stages.HardwareUnavailable, match="large \\(receiver\\) stage"):
        stages.open_stages(cfg)


# -- the right controller on each port ------------------------------------------------------


def pinned(receiver=16158, sample=33807):
    from dataclasses import replace

    return replace(stage_cfg(), serial_large=receiver, serial_small=sample)


def test_serial_problem_names_what_is_wrong():
    cfg = pinned()
    assert stages.serial_problem(FakeAxis(serial=33807), True, stage_cfg()) is None  # nothing pinned
    assert stages.serial_problem(FakeAxis(serial=16158), True, cfg) is None
    assert "the two ports are swapped" in stages.serial_problem(FakeAxis(serial=33807), True, cfg)
    assert "has controller 11111, but the receiver's is 16158" in stages.serial_problem(FakeAxis(serial=11111), True, cfg)

    class Unreadable(FakeAxis):
        def get_serial_number(self):
            raise RuntimeError("no answer")

    assert "could not be read" in stages.serial_problem(Unreadable(), False, cfg)


def test_open_stages_refuses_swapped_ports_before_writing_anything(monkeypatch):
    by_port = {"COM3": FakeAxis(serial=33807), "COM4": FakeAxis(serial=16158)}  # the lab PC's wiring
    monkeypatch.setattr(real_ximc, "Axis", lambda uri: by_port[uri])
    edges = {port: axis.edges for port, axis in by_port.items()}

    with pytest.raises(stages.HardwareUnavailable, match="COM3 \\(receiver\\): it has the sample's controller"):
        stages.open_stages(pinned())

    for port, axis in by_port.items():
        assert axis.calls[-1] == ("close",)
        assert axis.edges is edges[port] and axis.calibration == (1.0, 9)  # no limits, no calibration written


def test_open_stages_goes_ahead_with_the_right_controllers(monkeypatch):
    by_port = {"COM3": FakeAxis(serial=16158), "COM4": FakeAxis(serial=33807)}
    monkeypatch.setattr(real_ximc, "Axis", lambda uri: by_port[uri])

    large, small = stages.open_stages(pinned())

    assert (large.serial, small.serial) == (16158, 33807)
    assert large.calibration == (0.0072, 9)


def test_locate_controllers_changes_nothing_without_pinned_serials(monkeypatch):
    monkeypatch.setattr(real_ximc, "enumerate_devices", lambda *a: pytest.fail("listed the ports"))
    cfg = stage_cfg()
    assert stages.locate_controllers(cfg) == (cfg, [])


def test_locate_controllers_uses_the_port_each_pinned_controller_is_on(monkeypatch):
    listing = [{"uri": "xi-com:\\\\.\\COM4", "device_serial": 16158}, {"uri": "xi-com:\\\\.\\COM3", "device_serial": 33807}]
    monkeypatch.setattr(real_ximc, "enumerate_devices", lambda *a: listing)

    located, notes = stages.locate_controllers(pinned())

    assert (located.device_uri_large, located.device_uri_small) == ("xi-com:\\\\.\\COM4", "xi-com:\\\\.\\COM3")
    assert len(notes) == 2 and "using COM4" in notes[0]
