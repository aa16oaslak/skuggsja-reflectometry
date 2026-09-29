from __future__ import annotations

import time
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer

from . import clock, hardware, live_view, simulation, sweeps, toptica
from . import config as cfgmod
from .emergency_stop import run_with_emergency_stop
from .hardware_window import HardwareCheckWindow
from .stages import HardwareUnavailable, open_stages
from .stop_window import StopWindowUnavailable

app = typer.Typer(help="THz reflectometry stage + TOptica sweep control, with an on-screen emergency stop.")
config_app = typer.Typer(help="Manage the local configuration file.")
app.add_typer(config_app, name="config")


def _version_callback(show: bool) -> None:
    if show:
        typer.echo(_pkg_version("hi-skuggsja-reflectometry"))
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass

CONFIG_OPTION = typer.Option(
    None, "--config", help="Path to a config TOML overriding the packaged defaults."
)
GUI_OPTION = typer.Option(
    True, "--gui/--no-gui", help="Show the on-screen STOP button (default). With --no-gui, Ctrl+C is the stop."
)
SIMULATE_OPTION = typer.Option(
    False, "--simulate",
    help="Test run: simulated stages and TOptica, nothing connected or moved, with a live view of the setup.",
)
DEFAULT_SIM_SPEED = 20.0
SPEED_OPTION = typer.Option(
    None, "--speed",
    help=f"With --simulate: how many times faster than real time to run (default {DEFAULT_SIM_SPEED:g}).",
)
SIM_OUTPUT_DIR = Path("reflecto_simulated")
SKIP_CHECK_OPTION = typer.Option(
    False, "--skip-check",
    help="Start even if the hardware check before the sweep reports a problem. Only if you are sure the check is wrong.",
)


@app.command()
def check(
    gui: bool = typer.Option(True, "--gui/--no-gui", help="Show the result in a window (default), or just print it."),
    simulate: bool = typer.Option(False, "--simulate", help="Check a simulated setup instead, e.g. to try the window."),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Check that both stages and the TOptica are connected and ready. Reads status only: nothing moves."""
    cfg = cfgmod.load_config(config)
    rig = simulation.SimRig(cfg) if simulate else None
    try:
        run_check = (lambda: hardware.check_simulated(rig)) if rig else (lambda: hardware.check_hardware(cfg))
        if gui:
            statuses = HardwareCheckWindow(run_check, hardware.unchecked(cfg, simulate, "checking...")).run()
        else:
            statuses = run_check()
    except StopWindowUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)
    finally:
        if rig:
            rig.close()
    if statuses is None:  # the window was closed before the check finished
        raise typer.Exit(code=1)
    typer.echo(hardware.format_statuses(statuses))
    raise typer.Exit(code=0 if hardware.all_usable(statuses) else 1)


def _preflight(run_check, cfg: cfgmod.AppConfig, skip: bool, simulated: bool) -> list[hardware.DeviceStatus]:
    """The hardware check before a sweep. Exits, before anything has moved,
    if a device is not usable."""
    if skip:
        typer.echo("Skipping the hardware check (--skip-check).")
        return hardware.unchecked(cfg, simulated, "not checked (--skip-check)")
    typer.echo("Checking the hardware (reads status only, nothing moves)...")
    statuses = run_check()
    typer.echo(hardware.format_statuses(statuses))
    if not hardware.all_usable(statuses):
        typer.echo(
            "Error: the hardware check found a problem, so nothing was moved. Fix it and try again "
            "('reflecto check' shows the status), or add --skip-check if you are sure the check is wrong."
        )
        raise typer.Exit(code=1)
    return statuses


def _watch(axis) -> hardware.WatchedAxis:
    """Wraps a calibrated stage for the live view, and reads its status once
    so the view starts where the stage is."""
    watched = hardware.WatchedAxis(axis)
    try:
        watched.get_status()
    except Exception:  # noqa: BLE001, S110 -- shows as a red light, and the sweep's first move reports it too
        pass
    return watched


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing local config file."),
) -> None:
    """Copy the default config to ./reflecto.toml for editing."""
    dest = Path.cwd() / "reflecto.toml"
    if dest.exists() and not force:
        typer.echo(f"{dest} already exists; use --force to overwrite.")
        raise typer.Exit(code=1)
    cfgmod.write_default_config(dest)
    typer.echo(f"Wrote default config to {dest}")


def _run(
    sweep_fn,
    plan_fn,
    title: str,
    start_angle: float,
    end_angle: float,
    step: float,
    freq_start: float | None,
    freq_stop: float | None,
    int_time: float | None,
    filename: str,
    set_zero: bool,
    overwrite: bool,
    gui: bool,
    simulate: bool,
    speed: float | None,
    skip_check: bool,
    config_path: Path | None,
) -> None:
    if speed is not None and (not simulate or speed <= 0):
        typer.echo("Error: --speed needs --simulate and a positive number.")
        raise typer.Exit(code=1)

    cfg = cfgmod.load_config(config_path)
    freq_start = cfg.scan_defaults.freq_start if freq_start is None else freq_start
    freq_stop = cfg.scan_defaults.freq_stop if freq_stop is None else freq_stop
    int_time = cfg.scan_defaults.int_time if int_time is None else int_time

    try:
        # Fail on a filename collision before any hardware is opened; the
        # sweep re-checks this itself too.
        sweeps.check_outputs_available(
            filename, start_angle, end_angle, step, freq_start, freq_stop, int_time, overwrite
        )
    except FileExistsError as e:
        typer.echo(f"Error: {e}")
        if simulate:
            typer.echo("The real sweep would stop here too. The simulation writes nothing here; "
                       "add --overwrite to simulate anyway.")
        raise typer.Exit(code=1)

    toptica.link.reset()  # fresh live status for this sweep
    toptica.progress.reset()
    plan = plan_fn(start_angle, end_angle, step)

    if simulate:
        _simulate(
            sweep_fn, plan, title, cfg, start_angle, end_angle, step,
            freq_start, freq_stop, int_time, filename, set_zero, gui,
            DEFAULT_SIM_SPEED if speed is None else speed, skip_check,
        )
        return

    checked = _preflight(lambda: hardware.check_hardware(cfg), cfg, skip_check, simulated=False)

    try:
        large_stage, small_stage = open_stages(cfg.stages)
    except HardwareUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)

    large, small = _watch(large_stage), _watch(small_stage)
    files = [s.output_file(filename, int_time, freq_start, freq_stop) for s in plan]
    source = live_view.SweepSource(large, small, cfg.stages, checked, simulated=False)
    try:
        run_with_emergency_stop(
            sweep_fn,
            [large, small],
            list(sweeps.STAGE_NAMES),
            large,
            small,
            cfg,
            start_angle,
            end_angle,
            step,
            freq_start,
            freq_stop,
            int_time,
            filename,
            set_zero,
            overwrite,
            gui=gui,
            title=title,
            window=live_view.live_window(source, plan, files, cfg.stages),
        )
    except (FileExistsError, StopWindowUnavailable) as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)


def _simulate(
    sweep_fn,
    plan: list[sweeps.PlannedStep],
    title: str,
    cfg: cfgmod.AppConfig,
    start_angle: float,
    end_angle: float,
    step: float,
    freq_start: float,
    freq_stop: float,
    int_time: float,
    filename: str,
    set_zero: bool,
    gui: bool,
    speed: float,
    skip_check: bool,
) -> None:
    """Runs the same sweep code against simulated stages and TOptica, and
    prints planned-vs-actual angles for every step afterwards."""
    typer.echo(
        f"SIMULATION: no hardware is used. Stages and TOptica are simulated, time runs x{speed:g}, "
        f"and the photocurrent is made up. Files go to {SIM_OUTPUT_DIR}/."
    )
    with clock.accelerated(speed):
        rig = simulation.SimRig(cfg, SIM_OUTPUT_DIR)
        sim_filename = str(SIM_OUTPUT_DIR / filename)
        files = [s.output_file(sim_filename, int_time, freq_start, freq_stop) for s in plan]
        t0 = time.monotonic()
        ran = False
        try:
            checked = _preflight(lambda: hardware.check_simulated(rig), cfg, skip_check, simulated=True)
            large, small = _watch(rig.large), _watch(rig.small)
            source = live_view.SweepSource(large, small, cfg.stages, checked, simulated=True)
            run_with_emergency_stop(
                sweep_fn,
                [large, small],
                list(sweeps.STAGE_NAMES),
                large,
                small,
                rig.cfg,
                start_angle,
                end_angle,
                step,
                freq_start,
                freq_stop,
                int_time,
                sim_filename,
                set_zero,
                True,  # replace files from earlier simulations
                gui=gui,
                title=f"{title} (simulated)",
                window=live_view.live_window(source, plan, files, cfg.stages),
            )
            ran = True
        except StopWindowUnavailable as e:
            typer.echo(f"Error: {e}")
            raise typer.Exit(code=1)
        finally:
            rig.close()
            if ran or rig.toptica.scans or rig.large.moves or rig.small.moves:
                typer.echo(rig.summary(plan, [Path(f) for f in files], time.monotonic() - t0))


@app.command()
def spec(
    start_angle: float = typer.Option(..., help="Sweep start angle (deg)."),
    end_angle: float = typer.Option(..., help="Sweep end angle (deg)."),
    step: float = typer.Option(..., help="Angle step (deg)."),
    freq_start: float | None = typer.Option(None, help="Start frequency (GHz). Defaults from config."),
    freq_stop: float | None = typer.Option(None, help="Stop frequency (GHz). Defaults from config."),
    int_time: float | None = typer.Option(None, help="Lock-in integration time (ms). Defaults from config."),
    filename: str = typer.Option(..., help="Output filename prefix."),
    set_zero: bool = typer.Option(
        False, "--set-zero/--no-set-zero", help="Home and zero stages before the sweep."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow overwriting existing output files. Default: refuse to clobber."
    ),
    gui: bool = GUI_OPTION,
    simulate: bool = SIMULATE_OPTION,
    speed: float | None = SPEED_OPTION,
    skip_check: bool = SKIP_CHECK_OPTION,
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a specular (theta-2theta) reflectometry sweep."""
    title = f"Specular sweep: sample {start_angle}° to {end_angle}° in {step}° steps"
    _run(
        sweeps.sweep_spec, sweeps.plan_spec, title, start_angle, end_angle, step, freq_start, freq_stop,
        int_time, filename, set_zero, overwrite, gui, simulate, speed, skip_check, config,
    )


@app.command()
def nonspec(
    start_angle: float = typer.Option(..., help="Sweep start angle (deg)."),
    end_angle: float = typer.Option(..., help="Sweep end angle (deg)."),
    step: float = typer.Option(..., help="Angle step (deg)."),
    freq_start: float | None = typer.Option(None, help="Start frequency (GHz). Defaults from config."),
    freq_stop: float | None = typer.Option(None, help="Stop frequency (GHz). Defaults from config."),
    int_time: float | None = typer.Option(None, help="Lock-in integration time (ms). Defaults from config."),
    filename: str = typer.Option(..., help="Output filename prefix."),
    set_zero: bool = typer.Option(
        False, "--set-zero/--no-set-zero", help="Home and zero stages before the sweep."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow overwriting existing output files. Default: refuse to clobber."
    ),
    gui: bool = GUI_OPTION,
    simulate: bool = SIMULATE_OPTION,
    speed: float | None = SPEED_OPTION,
    skip_check: bool = SKIP_CHECK_OPTION,
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a non-specular reflectometry sweep (receiver stage only)."""
    title = f"Non-specular sweep: receiver {start_angle}° to {end_angle}° in {step}° steps"
    _run(
        sweeps.sweep_nonspec, sweeps.plan_nonspec, title, start_angle, end_angle, step, freq_start, freq_stop,
        int_time, filename, set_zero, overwrite, gui, simulate, speed, skip_check, config,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
