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


def test_confirm_path_clear_requires_exact_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "clear")
    assert stages.confirm_path_clear(30, 180, ZERO_L, -ZERO_S) is True

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert stages.confirm_path_clear(30, 180, ZERO_L, -ZERO_S) is False


def test_home_and_zero_cancelled_stops_before_moving_axis1(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    axis1, axis2 = FakeAxis(), FakeAxis()

    with pytest.raises(RuntimeError):
        stages.home_and_zero(axis1, axis2, "large", "small", ANGLE_MIN, ANGLE_MAX, 0.0072, ZERO_L, ZERO_S, threading.Event())

    assert ("homezero",) in axis2.calls  # small stage already homed
    assert axis1.calls == []  # large stage never touched


def test_home_and_zero_confirmed_homes_both_and_restores_edges(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "clear")
    axis1, axis2 = FakeAxis(), FakeAxis()
    original_left, original_right = axis1.edges.LeftBorder, axis1.edges.RightBorder

    stages.home_and_zero(axis1, axis2, "large", "small", ANGLE_MIN, ANGLE_MAX, 0.0072, ZERO_L, ZERO_S, threading.Event())

    assert ("homezero",) in axis1.calls
    assert ("homezero",) in axis2.calls
    assert axis1.edges.LeftBorder == original_left
    assert axis1.edges.RightBorder == original_right


def test_home_and_zero_does_nothing_if_already_stopped(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("should not prompt"))
    axis1, axis2 = FakeAxis(), FakeAxis()
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(RuntimeError, match="Emergency stop"):
        stages.home_and_zero(axis1, axis2, "large", "small", ANGLE_MIN, ANGLE_MAX, 0.0072, ZERO_L, ZERO_S, stop_event)

    assert axis1.calls == axis2.calls == []


def test_home_and_zero_stop_during_first_homing_skips_prompt_and_second_stage(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("should not prompt"))
    axis1, axis2 = FakeAxis(), FakeAxis()
    stop_event = threading.Event()
    axis2.command_homezero = stop_event.set  # STOP pressed while the small stage homes

    with pytest.raises(RuntimeError, match="zero may be wrong"):
        stages.home_and_zero(axis1, axis2, "large", "small", ANGLE_MIN, ANGLE_MAX, 0.0072, ZERO_L, ZERO_S, stop_event)

    assert axis1.calls == []


def test_home_and_zero_stop_pressed_at_prompt_still_blocks_second_homing(monkeypatch):
    stop_event = threading.Event()

    def type_clear_after_pressing_stop(_prompt):
        stop_event.set()
        return "clear"

    monkeypatch.setattr("builtins.input", type_clear_after_pressing_stop)
    axis1, axis2 = FakeAxis(), FakeAxis()

    with pytest.raises(RuntimeError, match="Emergency stop"):
        stages.home_and_zero(axis1, axis2, "large", "small", ANGLE_MIN, ANGLE_MAX, 0.0072, ZERO_L, ZERO_S, stop_event)

    assert ("homezero",) not in axis1.calls


def test_confirm_path_clear_treats_closed_input_as_cancel(monkeypatch):
    def closed(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert stages.confirm_path_clear(30, 180, ZERO_L, -ZERO_S) is False


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
