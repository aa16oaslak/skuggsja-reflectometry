from __future__ import annotations

from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer

from . import config as cfgmod
from . import sweeps
from .emergency_stop import run_with_emergency_stop
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
    config_path: Path | None,
) -> None:
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
        raise typer.Exit(code=1)

    try:
        large_stage, small_stage = open_stages(cfg.stages)
    except HardwareUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)

    try:
        run_with_emergency_stop(
            sweep_fn,
            [large_stage, small_stage],
            list(sweeps.STAGE_NAMES),
            large_stage,
            small_stage,
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
        )
    except (FileExistsError, StopWindowUnavailable) as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)


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
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a specular (theta-2theta) reflectometry sweep."""
    title = f"Specular sweep: sample {start_angle}° to {end_angle}° in {step}° steps"
    _run(sweeps.sweep_spec, title, start_angle, end_angle, step, freq_start, freq_stop, int_time, filename, set_zero, overwrite, gui, config)


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
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a non-specular reflectometry sweep (receiver stage only)."""
    title = f"Non-specular sweep: receiver {start_angle}° to {end_angle}° in {step}° steps"
    _run(sweeps.sweep_nonspec, title, start_angle, end_angle, step, freq_start, freq_stop, int_time, filename, set_zero, overwrite, gui, config)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
