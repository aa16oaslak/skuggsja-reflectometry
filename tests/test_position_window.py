"""Drives the real position window; skipped when there is no display."""
import pytest

from hi_skuggsja_reflectometry import clock, hardware, sweeps
from hi_skuggsja_reflectometry import config as cfgmod
from hi_skuggsja_reflectometry.arm_mover import ArmMover
from hi_skuggsja_reflectometry.position_window import PositionWindow, parse_turn_step
from hi_skuggsja_reflectometry.stages import RECEIVER, SAMPLE
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable


@pytest.fixture
def open_window(rig, tmp_path):
    """Opens position windows on the simulated rig; stops their readers after."""
    readers = []

    def make(**kw):
        reader = hardware.PositionReader(rig.large, rig.small, period=0.02).start()
        readers.append(reader)
        try:
            return PositionWindow(reader, rig.cfg, tmp_path / "reflecto.toml", **kw)
        except StopWindowUnavailable as exc:
            pytest.skip(f"no display for Tk: {exc}")

    yield make
    for reader in readers:
        reader.stop()


def then(window, action, delay=300):
    """Runs `action` once the window has been showing for `delay` ms."""
    window.root.after(delay, action)


def test_shows_both_arms_and_confirms_with_their_readings(rig, open_window):
    window = open_window(confirm=True, plan=sweeps.plan_spec(15, 30, 7.5))
    seen = {}

    def look_then_confirm():
        seen.update(readout=window.readout.cget("text"), ok=str(window.ok_button.cget("state")))
        window.ok_button.invoke()

    then(window, look_then_confirm)
    result = window.run()

    assert "Receiver R1     90.00°  homed" in seen["readout"]
    assert "Sample R2        0.00°  homed" in seen["readout"]
    assert seen["ok"] == "normal"
    assert result.confirmed
    assert result.readings[0].position == pytest.approx(rig.zero_l - 90)


def test_cancelling_returns_no_readings(open_window):
    window = open_window(confirm=True)
    then(window, window._cancel)

    result = window.run()

    assert not result.confirmed and result.readings is None


def test_not_homed_is_pointed_out_but_does_not_block(rig, open_window):
    rig.large.homed = False
    window = open_window(confirm=True)
    seen = {}
    then(window, lambda: (seen.update(notice=window.notice.cget("text"), ok=str(window.ok_button.cget("state"))),
                          window._cancel()))

    window.run()

    assert "Not homed" in seen["notice"] and "reflecto homing" in seen["notice"]
    assert seen["ok"] == "normal"


def test_a_stage_that_stops_answering_blocks_confirming(rig, open_window):
    window = open_window(confirm=True)
    seen = {}
    then(window, rig.large.close_device, delay=100)  # as if the cable were pulled
    then(window, lambda: (seen.update(notice=window.notice.cget("text"), ok=str(window.ok_button.cget("state"))),
                          window.ok_button.invoke(), window._cancel()), delay=500)

    result = window.run()

    assert "Not answering" in seen["notice"]
    assert seen["ok"] == "disabled"
    assert not result.confirmed


def test_a_moving_stage_blocks_confirming_until_it_stops(rig, open_window):
    window = open_window(confirm=True)
    seen = {}

    def start_a_long_move():
        with clock.accelerated(1):
            rig.large.command_move_calb(rig.zero_l - 170)  # 80° at 5°/s: moving for the whole test

    then(window, start_a_long_move, delay=50)
    then(window, lambda: (seen.update(notice=window.notice.cget("text"), ok=str(window.ok_button.cget("state"))),
                          window._cancel()), delay=400)
    window.run()
    rig.large.command_stop()

    assert "moving" in seen["notice"]
    assert seen["ok"] == "disabled"


def type_step(window, text):
    window.step_entry.delete(0, "end")
    window.step_entry.insert(0, text)


@pytest.mark.parametrize(
    "text, step", [("5", 5.0), ("0.5", 0.5), ("2,5", 2.5), (" 360 ", 360.0), ("0", None), ("-3", None), ("400", None), ("five", None)]
)
def test_parse_turn_step(text, step):
    assert parse_turn_step(text) == step


def test_turn_by_any_step_both_ways(open_window):
    window = open_window()
    seen = {}

    def turn():
        assert window.step_entry.get() == "5"  # the default step
        type_step(window, "2,5")
        window.ccw_button.invoke()  # 2.5
        window.ccw_button.invoke()  # 5
        type_step(window, "0.5")
        window.cw_button.invoke()  # 4.5
        seen["direction"] = window.direction_label.cget("text")
        window.cw_button.invoke()  # 4
        window.cw_button.invoke()  # 3.5
        type_step(window, "10")
        window.cw_button.invoke()  # -6.5 -> 353.5
        seen["wrapped"] = window.view.tx_direction
        type_step(window, "a lot")
        window.cw_button.invoke()  # refused
        seen["refused"] = (window.view.tx_direction, window.direction_label.cget("text"))
        window._cancel()

    then(window, turn)
    window.run()

    assert "Tx is drawn at 4.5°" in seen["direction"]
    assert seen["wrapped"] == pytest.approx(353.5)
    assert seen["refused"][0] == pytest.approx(353.5)
    assert "more than 0 and at most 360" in seen["refused"][1]


def test_mirror_turn_and_save_the_drawing(rig, open_window, tmp_path):
    window = open_window()
    seen = {}

    def adjust_and_save():
        window._mirror()
        type_step(window, "90")
        window.ccw_button.invoke()
        window.ccw_button.invoke()
        seen["before_save"] = window.save_status.cget("text")
        window.save_button.invoke()
        seen["after_save"] = window.save_status.cget("text")
        window._cancel()

    then(window, adjust_and_save)
    result = window.run()

    assert result.view == cfgmod.ViewConfig(receiver_turns="counterclockwise", tx_direction=180)
    assert window.drawing.view == result.view
    assert "this run only" in seen["before_save"] and "Saved in" in seen["after_save"]
    assert cfgmod.load_config(tmp_path / "reflecto.toml").view == result.view


def test_position_command_opens_the_window(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hi_skuggsja_reflectometry import cli

    monkeypatch.chdir(tmp_path)
    original_run = PositionWindow.run

    def run_then_close(self):
        self.root.after(800, self._cancel)
        return original_run(self)

    monkeypatch.setattr(PositionWindow, "run", run_then_close)
    result = CliRunner().invoke(cli.app, ["position", "--simulate"])
    if "Could not open the position window" in result.output:
        pytest.skip("no display for Tk")

    assert result.exit_code == 0, result.output


# -- turning the real arms from the window -----------------------------------------------


def wait_for_move(window, mover, then_do):
    """Calls then_do() once the mover has finished its move."""

    def check():
        if mover.state()[0] is None:
            then_do()
        else:
            window.root.after(20, check)

    window.root.after(20, check)


def test_moves_are_locked_until_allowed(rig, open_window):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    window = open_window(mover=mover)
    seen = {}

    def look():
        window._refresh()
        seen["before"] = (window.banner.cget("text"), str(window.arm_buttons[RECEIVER][0].cget("state")))
        window.arm_buttons[RECEIVER][0].invoke()  # ignored while not allowed
        window.allow_moves.set(True)
        window._allow_changed()
        window._refresh()
        seen["after"] = (window.banner.cget("text"), str(window.arm_buttons[RECEIVER][0].cget("state")))
        window._cancel()

    then(window, look)
    window.run()

    assert seen["before"] == ("READ ONLY - nothing moves", "disabled")
    assert seen["after"] == ("MOVES ALLOWED - the real arms can move", "normal")
    assert rig.large.moves == 0


@pytest.mark.parametrize("stage", [RECEIVER, SAMPLE])
def test_arms_turn_the_way_the_drawing_shows_and_follow_mirror(rig, open_window, stage):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    window = open_window(mover=mover)
    index = 1 if stage == RECEIVER else 0  # rig.angles() is (sample, receiver)
    seen = [rig.angles()[index]]

    def step(actions):
        if not actions:
            window._cancel()
            return
        action, rest = actions[0], actions[1:]
        action()
        wait_for_move(window, mover, lambda: (seen.append(rig.angles()[index]), step(rest)))

    def start():
        window.allow_moves.set(True)
        window._allow_changed()
        window.move_entry.delete(0, "end")
        window.move_entry.insert(0, "2,5")
        clockwise = window.arm_buttons[stage][1]
        step([
            clockwise.invoke,  # the drawing grows clockwise: +2.5°
            lambda: (window._mirror(), clockwise.invoke()),  # now it grows counterclockwise: -2.5°
        ])

    then(window, start)
    window.run()

    assert seen[1] - seen[0] == pytest.approx(2.5)
    assert seen[2] - seen[1] == pytest.approx(-2.5)


def test_a_bad_step_is_refused_in_the_window(rig, open_window):
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    window = open_window(mover=mover)
    seen = {}

    def try_it():
        window.allow_moves.set(True)
        window._allow_changed()
        for text in ("15", "abc"):
            window.move_entry.delete(0, "end")
            window.move_entry.insert(0, text)
            window.arm_buttons[RECEIVER][1].invoke()
            seen[text] = window.move_status.cget("text")
        window._cancel()

    then(window, try_it)
    window.run()

    assert "at most 10" in seen["15"] and "at most 10" in seen["abc"]
    assert rig.large.moves == 0


def test_closing_the_window_stops_a_move(rig, open_window):
    rig.large.speed = 0.5  # 5° takes 10 s
    mover = ArmMover(rig.large, rig.small, rig.cfg.stages)
    window = open_window(mover=mover)
    seen = {}

    def move_then_close():
        window.allow_moves.set(True)
        window._allow_changed()
        window.move_entry.delete(0, "end")
        window.move_entry.insert(0, "5")
        window.arm_buttons[RECEIVER][0].invoke()
        seen["moving"] = rig.large.moving
        window.root.after(200, window._cancel)

    with clock.accelerated(1):  # real time: the rig fixture otherwise runs the clock fast
        then(window, move_then_close)
        window.run()
        mover.close()

    assert seen["moving"] is True
    assert not rig.large.moving


def test_the_step_before_a_sweep_offers_no_moves(open_window):
    window = open_window(confirm=True)
    try:
        assert window.move_buttons == []
        assert window.banner.cget("text") == "READ ONLY - nothing moves"
    finally:
        window.root.destroy()


def test_phi_angles_stay_within_a_turn_when_the_sample_count_passes_one(cfg, tmp_path):
    from hi_skuggsja_reflectometry import simulation
    from hi_skuggsja_reflectometry.hardware import ArmReading

    # the lab screenshot: sample count 341.53 (= 381.53°), receiver at 112.09°
    rig = simulation.SimRig(cfg, start=(ArmReading(68.41, False, False), ArmReading(341.53, False, False)))
    reader = hardware.PositionReader(rig.large, rig.small).start()
    try:
        window = PositionWindow(reader, cfg, tmp_path / "reflecto.toml")
    except StopWindowUnavailable as exc:
        reader.stop()
        rig.close()
        pytest.skip(f"no display for Tk: {exc}")
    try:
        text = window.readout.cget("text")
        canvas = window.drawing.canvas
        extents = [float(canvas.itemcget(item, "extent")) for item in canvas.find_all() if canvas.type(item) == "arc"]
        labels = [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]
    finally:
        window.root.destroy()
        reader.stop()
        rig.close()

    assert "Sample R2       21.53°" in text and "at 341.53°" in text  # the angle within a turn, the raw count too
    assert "φ1 incidence    21.53°" in text and "φ2 receiver     90.56°" in text
    assert "φ1 21.5°" in labels and "φ2 90.6°" in labels
    assert extents and all(abs(e) <= 180 for e in extents)  # no arc goes more than half way round
