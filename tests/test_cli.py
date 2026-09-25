import socket
import sys

import pytest
from typer.testing import CliRunner

from hi_skuggsja_reflectometry import cli
from hi_skuggsja_reflectometry.stop_window import StopWindowUnavailable
from tests.fakes import FakeAxis

runner = CliRunner()


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
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "myrun"],
    )

    assert result.exit_code == 0, result.output
    assert captured["sweep_fn"].__name__ == "sweep_spec"
    assert captured["axes"] == [large, small]

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
            "--overwrite", "--no-gui",
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
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run"],
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
        ["spec", "--start-angle", "30", "--end-angle", "30", "--step", "7.5", "--filename", "run"],
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


def test_speed_needs_simulate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, [*SIM_ARGS, "--speed", "5"])
    assert result.exit_code == 1
    assert "--speed needs --simulate" in result.output


def test_simulate_reports_a_collision_the_real_sweep_would_hit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "run_15.0degrees_3ms_70.0GHz_to_70.5.txt").write_text("real data\n")

    result = runner.invoke(cli.app, [*SIM_ARGS, "--simulate", "--no-gui"])

    assert result.exit_code == 1
    assert "real sweep would stop here too" in result.output
    assert (tmp_path / "run_15.0degrees_3ms_70.0GHz_to_70.5.txt").read_text() == "real data\n"
