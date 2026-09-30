import socket
import sys
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from hi_skuggsja_reflectometry import cli
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.fakes import FakeAxis

runner = CliRunner()


@pytest.fixture(autouse=True)
def hardware_ok(monkeypatch):
    """The pre-sweep hardware check passes, without touching real ports."""
    from hi_skuggsja_reflectometry import hardware

    monkeypatch.setattr(
        cli.hardware, "check_hardware",
        lambda cfg: [hardware.DeviceStatus(n, "test", hardware.OK, "ready") for n in ("R1", "R2", "TOptica")],
    )


def test_help_lists_subcommands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "spec" in result.output
    assert "nonspec" in result.output
    assert "config" in result.output


def test_config_init_writes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0
    assert (tmp_path / "reflecto.toml").exists()


def test_config_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reflecto.toml").write_text("stale\n")

    result = runner.invoke(cli.app, ["config", "init"])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert (tmp_path / "reflecto.toml").read_text() == "stale\n"


def test_config_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reflecto.toml").write_text("stale\n")

    result = runner.invoke(cli.app, ["config", "init", "--force"])

    assert result.exit_code == 0
    assert (tmp_path / "reflecto.toml").read_text() != "stale\n"


def test_spec_command_wires_config_defaults_and_calls_sweep(monkeypatch):
    large, small = FakeAxis(), FakeAxis()
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: (large, small))

    captured = {}

    def fake_run_with_emergency_stop(sweep_fn, axes, names, *sweep_args, **kwargs):
        captured["sweep_fn"] = sweep_fn
        captured["axes"] = axes
        captured["sweep_args"] = sweep_args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "run_with_emergency_stop", fake_run_with_emergency_stop)

    result = runner.invoke(
        cli.app,
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "myrun", "--skip-preview"],
    )

    assert result.exit_code == 0, result.output
    assert captured["sweep_fn"].__name__ == "sweep_spec"
    assert [axis.inner for axis in captured["axes"]] == [large, small]  # wrapped for the live view

    (_large, _small, cfg, start_angle, end_angle, step,
     freq_start, freq_stop, int_time, filename, set_zero, overwrite) = captured["sweep_args"]
    assert (start_angle, end_angle, step) == (30.0, 30.0, 7.5)
    assert filename == "myrun"
    assert set_zero is False
    assert overwrite is False
    assert captured["kwargs"]["gui"] is True  # STOP window is on by default
    assert "Specular sweep" in captured["kwargs"]["title"]
    # freq/int-time weren't passed on the command line, so they fall back to config
    assert (freq_start, freq_stop, int_time) == (cfg.scan_defaults.freq_start, cfg.scan_defaults.freq_stop, cfg.scan_defaults.int_time)


def test_nonspec_command_overrides_freq_defaults_when_given(monkeypatch):
    large, small = FakeAxis(), FakeAxis()
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: (large, small))

    captured = {}
    monkeypatch.setattr(
        cli, "run_with_emergency_stop",
        lambda sweep_fn, axes, names, *args, **kwargs: captured.update(sweep_fn=sweep_fn, sweep_args=args, kwargs=kwargs),
    )

    result = runner.invoke(
        cli.app,
        [
            "nonspec", "--start-angle", "45", "--end-angle", "75", "--step", "15",
            "--filename", "myrun", "--freq-start", "80", "--freq-stop", "300",
            "--overwrite", "--no-gui", "--skip-preview",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["sweep_fn"].__name__ == "sweep_nonspec"
    sweep_args = captured["sweep_args"]
    freq_start, freq_stop = sweep_args[6], sweep_args[7]
    overwrite = sweep_args[11]
    assert (freq_start, freq_stop) == (80.0, 300.0)
    assert overwrite is True
    assert captured["kwargs"]["gui"] is False


def test_spec_command_reports_file_exists_error_cleanly(monkeypatch):
    large, small = FakeAxis(), FakeAxis()
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: (large, small))

    def raise_collision(*args, **kwargs):
        raise FileExistsError("Output file already exists: run_30.0degrees_3ms_70GHz_to_400.txt")

    monkeypatch.setattr(cli, "run_with_emergency_stop", raise_collision)

    result = runner.invoke(
        cli.app,
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run", "--skip-preview"],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_spec_command_requires_filename():
    result = runner.invoke(cli.app, ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5"])
    assert result.exit_code != 0


def test_help_works_even_if_libximc_native_library_is_missing(monkeypatch):
    # --help never opens hardware, so it must survive even if the Standa
    # driver isn't installed on this machine.
    monkeypatch.setitem(sys.modules, "libximc.highlevel", None)

    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0


def test_config_init_works_even_if_libximc_native_library_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "libximc.highlevel", None)

    result = runner.invoke(cli.app, ["config", "init"])

    assert result.exit_code == 0
    assert (tmp_path / "reflecto.toml").exists()


def test_spec_reports_missing_libximc_library_cleanly_instead_of_a_traceback(monkeypatch):
    monkeypatch.setitem(sys.modules, "libximc.highlevel", None)

    result = runner.invoke(
        cli.app,
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run"],
    )

    assert result.exit_code == 1
    assert "Could not load the Standa libximc native library" in result.output
    # a controlled typer.Exit(1), not some other unhandled exception type
    assert isinstance(result.exception, SystemExit)


def test_spec_refuses_existing_output_before_opening_any_hardware(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "run_30.0degrees_3ms_70GHz_to_400.txt").write_text("previous data\n")
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened hardware"))

    result = runner.invoke(
        cli.app,
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run"],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_spec_reports_missing_display_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    large, small = FakeAxis(), FakeAxis()
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: (large, small))

    def no_window(*args, **kwargs):
        raise StopWindowUnavailable("Could not open the stop window (no display?).")

    monkeypatch.setattr(cli, "run_with_emergency_stop", no_window)

    result = runner.invoke(
        cli.app,
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run", "--skip-preview"],
    )

    assert result.exit_code == 1
    assert "Could not open the stop window" in result.output
    assert isinstance(result.exception, SystemExit)


SIM_ARGS = [
    "spec", "--start-angle", "15", "--end-angle", "22.5", "--step", "7.5",
    "--freq-start", "70", "--freq-stop", "70.5", "--filename", "run",
]


def test_simulate_runs_a_whole_sweep_without_touching_hardware(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened the real stages"))
    real_connect = socket.socket.connect

    def localhost_only(sock, address):
        assert address[0] == "127.0.0.1", f"connected to {address}"
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", localhost_only)

    result = runner.invoke(cli.app, [*SIM_ARGS, "--simulate", "--no-gui", "--speed", "1000"])

    assert result.exit_code == 0, result.output
    assert "no hardware was used" in result.output
    assert sorted(p.name for p in (tmp_path / "reflecto_simulated").iterdir()) == [
        "run_15.0degrees_3ms_70.0GHz_to_70.5.txt",
        "run_22.5degrees_3ms_70.0GHz_to_70.5.txt",
    ]
    assert not list(tmp_path.glob("*.txt"))  # nothing where real data goes


def test_speed_only_goes_with_a_simulation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for extra in (["--skip-preview"], ["--simulate", "--speed", "0"]):
        speed = [] if "--speed" in extra else ["--speed", "5"]
        result = runner.invoke(cli.app, [*SIM_ARGS, *extra, *speed])
        assert result.exit_code == 1
        assert "--speed sets how fast simulations run" in result.output


def test_simulate_reports_a_collision_the_real_sweep_would_hit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "run_15.0degrees_3ms_70.0GHz_to_70.5.txt").write_text("real data\n")

    result = runner.invoke(cli.app, [*SIM_ARGS, "--simulate", "--no-gui"])

    assert result.exit_code == 1
    assert "real sweep would stop here too" in result.output
    assert (tmp_path / "run_15.0degrees_3ms_70.0GHz_to_70.5.txt").read_text() == "real data\n"


def hardware_fails(monkeypatch):
    from hi_skuggsja_reflectometry import hardware

    monkeypatch.setattr(
        cli.hardware, "check_hardware",
        lambda cfg: [hardware.DeviceStatus("TOptica", "x:1", hardware.FAIL, "no answer at x:1")],
    )


def test_check_command_on_a_simulated_setup():
    result = runner.invoke(cli.app, ["check", "--no-gui", "--simulate"])
    assert result.exit_code == 0, result.output
    assert result.output.count("[ ok ]") == 3


def test_check_command_exits_1_when_something_is_not_ready(monkeypatch):
    hardware_fails(monkeypatch)
    result = runner.invoke(cli.app, ["check", "--no-gui"])
    assert result.exit_code == 1
    assert "[FAIL] TOptica" in result.output


def test_sweep_refuses_to_start_if_the_hardware_check_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hardware_fails(monkeypatch)
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened the stages"))

    result = runner.invoke(cli.app, ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "r"])

    assert result.exit_code == 1
    assert "nothing was moved" in result.output


def test_skip_check_starts_without_checking(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.hardware, "check_hardware", lambda cfg: pytest.fail("checked anyway"))
    large, small = FakeAxis(), FakeAxis()
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: (large, small))
    ran = []
    monkeypatch.setattr(cli, "run_with_emergency_stop", lambda *args, **kwargs: ran.append(True))

    result = runner.invoke(
        cli.app, ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "r", "--skip-check", "--skip-preview"]
    )

    assert result.exit_code == 0, result.output
    assert ran == [True]


# -- the three steps before a real sweep, answered in the terminal ---------------------

PREVIEW_ARGS = [
    "spec", "--start-angle", "15", "--end-angle", "22.5", "--step", "7.5",
    "--freq-start", "70", "--freq-stop", "70.5", "--filename", "run", "--no-gui", "--speed", "1000",
]
MOVES = ("move_calb", "movr_calb", "homezero", "stop")


@pytest.fixture
def stages_at(monkeypatch):
    """Replaces the real stages with fakes at the given controller positions:
    the pair read in step 1 and, unless `moved_to` says otherwise, the same
    pair for the sweep in step 3."""

    def place(receiver_position, sample_position, moved_to=None):
        read = (FakeAxis(position=receiver_position), FakeAxis(position=sample_position))
        swept = read if moved_to is None else (FakeAxis(position=moved_to[0]), FakeAxis(position=moved_to[1]))

        @contextmanager
        def open_readonly(stage_cfg):
            yield read

        monkeypatch.setattr(cli.hardware, "open_stages_readonly", open_readonly)
        monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: swept)
        return read

    return place


@pytest.fixture
def real_runs(monkeypatch):
    """Records the sweep sent to the real hardware; simulations run for real."""
    runs = []
    real = cli.run_with_emergency_stop

    def run(sweep_fn, axes, names, *args, **kwargs):
        if kwargs["title"].endswith("(simulated)"):
            return real(sweep_fn, axes, names, *args, **kwargs)
        runs.append(args)
        return None

    monkeypatch.setattr(cli, "run_with_emergency_stop", run)
    return runs


def test_real_sweep_shows_the_arms_simulates_from_there_then_asks(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    receiver, sample = stages_at(2.4, -25.0)  # receiver 178.10°, sample 15.00°

    result = runner.invoke(cli.app, PREVIEW_ARGS, input="yes\nrun\n")

    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("Step 1 of 3") < out.index("Step 2 of 3") < out.index("Step 3 of 3")
    assert "Receiver confirmed at 178.10°" in out and "Sample confirmed at 15.00°" in out
    assert "All 2 steps were scanned at the planned angles." in out
    assert len(real_runs) == 1
    assert not any(call[0] in MOVES for axis in (receiver, sample) for call in axis.calls)  # steps 1-2 moved nothing


def test_answering_no_about_the_arms_ends_it_before_the_simulation(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    stages_at(2.4, -25.0)
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened the stages to move them"))

    result = runner.invoke(cli.app, PREVIEW_ARGS, input="no\n")

    assert result.exit_code == 1
    assert "Cancelled. Nothing was moved." in result.output
    assert "Step 2 of 3" not in result.output
    assert real_runs == []


def test_answering_no_after_the_simulation_moves_nothing(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    stages_at(2.4, -25.0)
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened the stages to move them"))

    result = runner.invoke(cli.app, PREVIEW_ARGS, input="yes\nno\n")

    assert result.exit_code == 1
    assert "Cancelled. Nothing was moved." in result.output and "Step 3 of 3" not in result.output
    assert real_runs == []


def test_a_stage_that_moved_after_it_was_confirmed_stops_the_sweep(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    stages_at(2.4, -25.0, moved_to=(12.4, -25.0))  # e.g. someone jogged the receiver in XILab meanwhile

    result = runner.invoke(cli.app, PREVIEW_ARGS, input="yes\nrun\n")

    assert result.exit_code == 1
    assert "moved after its position was confirmed" in result.output
    assert "receiver 2.40° then, 12.40° now" in result.output
    assert real_runs == []


def test_a_sweep_that_fails_in_the_simulation_is_not_offered_for_the_hardware(
    tmp_path, monkeypatch, stages_at, real_runs
):
    monkeypatch.chdir(tmp_path)
    stages_at(2.4, -25.0)
    args = [a if a != "22.5" else "95" for a in PREVIEW_ARGS]
    args[args.index("--start-angle") + 1] = "80"  # receiver 160° to 190°, past the 180° limit

    result = runner.invoke(cli.app, args, input="yes\nrun\n")

    assert result.exit_code == 1
    assert "The simulated sweep failed" in result.output
    assert "Type 'run'" not in result.output
    assert real_runs == []


def test_skip_preview_goes_straight_to_the_hardware(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    stages_at(2.4, -25.0)
    args = [a for a in PREVIEW_ARGS if a not in ("--speed", "1000")]

    result = runner.invoke(cli.app, [*args, "--skip-preview"])

    assert result.exit_code == 0, result.output
    assert "Step 1 of 3" not in result.output
    assert len(real_runs) == 1


def test_position_prints_where_the_arms_are(stages_at):
    stages_at(2.4, 188.73)

    result = runner.invoke(cli.app, ["position", "--no-gui"])

    assert result.exit_code == 0, result.output
    assert "178.10°" in result.output and "228.73°" in result.output


def test_position_on_a_simulated_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["position", "--no-gui", "--simulate"])
    assert result.exit_code == 0, result.output
    assert "90.00°" in result.output and "0.00°" in result.output


def test_a_mistake_in_the_settings_file_is_reported_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reflecto.toml").write_text("[stages]\nzero_L = 181\n")

    result = runner.invoke(cli.app, ["position", "--no-gui", "--simulate"])

    assert result.exit_code == 1
    assert "unknown setting zero_L under [stages]" in result.output
    assert isinstance(result.exception, SystemExit)


# -- reflecto homing, and --set-zero -----------------------------------------------------


@pytest.fixture
def stages_ok(monkeypatch):
    from hi_skuggsja_reflectometry import hardware

    monkeypatch.setattr(
        cli.hardware, "check_stages",
        lambda cfg: [hardware.DeviceStatus(n, "test", hardware.WARN, "not homed") for n in ("R1", "R2")],
    )


def test_homing_a_simulated_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["homing", "--simulate", "--no-gui"], input="clear\n")

    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("About to home: Sample R2, then Receiver R1.") < out.index("Homing finished.")
    assert "Homing starts turning the way its angle" in out
    assert "180.50°" in out and "40.00°" in out  # both at their homes afterwards
    assert "NOT homed" not in out.split("Homing finished.")[1]


def test_homing_only_the_receiver(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["homing", "--simulate", "--no-gui", "--stage", "receiver"], input="clear\n")

    assert result.exit_code == 0, result.output
    after = result.output.split("Homing finished.")[1]
    assert "Sample R2" not in result.output.split("Homing finished.")[0].split("About to home:")[1].split(".")[0]
    assert "180.50°" in after
    assert "NOT homed" in after  # the sample was left as it was


def test_homing_moves_nothing_unless_clear_is_typed(tmp_path, monkeypatch, stages_at, stages_ok):
    monkeypatch.chdir(tmp_path)
    receiver, sample = stages_at(150.49, 17.59)
    monkeypatch.setattr(cli, "open_stages", lambda stage_cfg: pytest.fail("opened the stages to move them"))

    result = runner.invoke(cli.app, ["homing", "--no-gui"], input="yes\n")

    assert result.exit_code == 1
    assert "Cancelled. Nothing was moved." in result.output
    assert "30.01°" in result.output and "57.59°" in result.output  # where they were said to be
    assert not any(call[0] in (*MOVES, "home", "zero") for axis in (receiver, sample) for call in axis.calls)


def test_homing_real_stages(tmp_path, monkeypatch, stages_at, stages_ok):
    monkeypatch.chdir(tmp_path)
    receiver, sample = stages_at(150.49, 17.59)

    result = runner.invoke(cli.app, ["homing", "--no-gui"], input="clear\n")

    assert result.exit_code == 0, result.output
    for axis in (receiver, sample):
        kinds = [call[0] for call in axis.calls]
        assert kinds.index("home") < kinds.index("zero")


def test_homing_that_the_controller_does_not_finish_exits_with_an_error(tmp_path, monkeypatch, stages_at, stages_ok):
    monkeypatch.chdir(tmp_path)
    receiver, _sample = stages_at(150.49, 17.59)
    real_status = receiver.get_status

    def never_homed():
        status = real_status()
        status.Flags = 0
        return status

    receiver.get_status = never_homed

    result = runner.invoke(cli.app, ["homing", "--no-gui"], input="clear\n")

    assert result.exit_code == 1
    assert "Not homed: Receiver R1" in result.output
    assert ("zero",) not in receiver.calls


def test_set_zero_asks_before_the_real_sweep_moves_anything(tmp_path, monkeypatch, stages_at, real_runs):
    monkeypatch.chdir(tmp_path)
    receiver, sample = stages_at(150.49, 17.59)
    args = [a for a in PREVIEW_ARGS if a not in ("--speed", "1000")] + ["--skip-preview", "--set-zero"]

    result = runner.invoke(cli.app, args, input="no\n")

    assert result.exit_code == 1
    assert "About to home" in result.output and "Cancelled. Nothing was moved." in result.output
    assert real_runs == []
    assert not any(call[0] in (*MOVES, "home", "zero") for axis in (receiver, sample) for call in axis.calls)

    result = runner.invoke(cli.app, args, input="clear\n")
    assert result.exit_code == 0, result.output
    assert len(real_runs) == 1


def test_swapped_ports_stop_a_command_before_anything_moves(tmp_path, monkeypatch):
    import libximc.highlevel as real_ximc

    monkeypatch.chdir(tmp_path)
    (tmp_path / "reflecto.toml").write_text("[stages]\nserial_large = 16158\nserial_small = 33807\n")
    by_port = {"COM3": FakeAxis(serial=33807), "COM4": FakeAxis(serial=16158)}
    monkeypatch.setattr(real_ximc, "Axis", lambda uri: by_port[uri.rsplit("\\", 1)[-1]])

    result = runner.invoke(cli.app, ["position", "--no-gui"])

    assert result.exit_code == 1
    assert "the two ports are swapped" in result.output and "Nothing was moved" in result.output
