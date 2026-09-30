from __future__ import annotations

import gc
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from enum import Enum
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import typer

from . import clock, hardware, live_view, simulation, sweeps, toptica
from . import config as cfgmod
from .emergency_stop import run_with_emergency_stop
from .hardware_window import HardwareCheckWindow
from .homing_window import HomingProgress, homing_window
from .position_window import PositionResult, PositionWindow
from .stages import (
    HOMING_ORDER,
    RECEIVER,
    SAMPLE,
    STAGE_LABELS,
    HardwareUnavailable,
    home_stages,
    homing_direction,
    open_stages,
)
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
    help=(
        "How many times faster than real time simulations run: with --simulate, and the simulation "
        f"before a real sweep (default {DEFAULT_SIM_SPEED:g})."
    ),
)
SIM_OUTPUT_DIR = Path("reflecto_simulated")
SKIP_CHECK_OPTION = typer.Option(
    False, "--skip-check",
    help="Start even if the hardware check before the sweep reports a problem. Only if you are sure the check is wrong.",
)
SKIP_PREVIEW_OPTION = typer.Option(
    False, "--skip-preview",
    help=(
        "Real sweeps: go straight to the hardware, without first showing where the arms are and "
        "simulating the sweep from there. Only once both have been checked."
    ),
)
POSITION_TOLERANCE = 0.05  # degrees a stage may differ from where the simulation started it


def _load_config(path: Path | None) -> cfgmod.AppConfig:
    try:
        return cfgmod.load_config(path)
    except cfgmod.ConfigError as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1) from None


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:  # no terminal to answer from
        return ""


@app.command()
def check(
    gui: bool = typer.Option(True, "--gui/--no-gui", help="Show the result in a window (default), or just print it."),
    simulate: bool = typer.Option(False, "--simulate", help="Check a simulated setup instead, e.g. to try the window."),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Check that both stages and the TOptica are connected and ready. Reads status only: nothing moves."""
    cfg = _load_config(config)
    rig = simulation.SimRig(cfg) if simulate else None
    try:
        run_check = (lambda: hardware.check_simulated(rig)) if rig else (lambda: hardware.check_hardware(cfg))
        if gui:
            statuses = HardwareCheckWindow(run_check, hardware.unchecked(cfg, simulate, "checking...")).run()
            gc.collect()  # free the window's Tk objects here, on the main thread
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


@contextmanager
def _readable_stages(
    cfg: cfgmod.AppConfig, simulated: bool, rig: simulation.SimRig | None = None
) -> Iterator[tuple[Any, Any]]:
    """Both stages, opened to be read without moving: the real ones, or a
    simulated pair (`rig`'s, if given, which is left for the caller to close)."""
    if rig is not None:
        for axis in (rig.large, rig.small):
            axis.open_device()
        yield rig.large, rig.small
        return
    if simulated:
        rig = simulation.SimRig(cfg)
        try:
            yield rig.large, rig.small
        finally:
            rig.close()
        return
    try:
        with hardware.open_stages_readonly(cfg.stages) as axes:
            yield axes
    except HardwareUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1) from None


def _position_window(reader: hardware.PositionReader, cfg: cfgmod.AppConfig, config_path: Path | None, **kw: Any) -> PositionResult:
    try:
        window = PositionWindow(reader, cfg, cfgmod.settings_path(config_path), **kw)
    except StopWindowUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1) from None
    result = window.run()
    # Free the closed window's Tk objects here, on the main thread, with the
    # reader thread stopped first: Tk objects freed by a garbage collection
    # on any other thread abort the whole process.
    reader.stop()
    del window
    gc.collect()
    return result


@app.command()
def position(
    gui: bool = typer.Option(True, "--gui/--no-gui", help="Show the drawing (default), or just print the angles."),
    simulate: bool = typer.Option(False, "--simulate", help="Read a simulated setup instead, e.g. to try the window."),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Show where both arms are now, read from the stages without moving them. The drawing can be
    mirrored and turned to match the table, and saved."""
    cfg = _load_config(config)
    with _readable_stages(cfg, simulate) as axes:
        reader = hardware.PositionReader(*axes).start()
        try:
            if gui:
                _position_window(reader, cfg, config, simulated=simulate)
            else:
                typer.echo("Where the arms are now (read from the stages; nothing moved):")
                typer.echo(hardware.format_readings(*reader.latest(), cfg.stages))
        finally:
            reader.stop()


def _confirm_homing(
    stages: list[str], readings: list[hardware.ArmReading], directions: dict[str, int | None], cfg: cfgmod.AppConfig
) -> bool:
    """Says what homing will do, and asks for 'clear' before anything moves."""
    order = [s for s in HOMING_ORDER if s in stages]
    typer.echo(
        "\nAbout to home: " + ", then ".join(STAGE_LABELS[s] for s in order) + ".\n"
        "Each stage turns, with its controller's own homing settings, until it finds its home sensor "
        "(that can be most of a full turn), and only then is its count set to 0 there."
    )
    for stage in order:
        large = stage == RECEIVER
        reading = readings[0 if large else 1]
        d = directions.get(stage)
        if d is None:
            way = "in a direction that could not be read from its controller"
        else:
            grows = (d > 0) != large  # a higher count is a smaller receiver angle, a larger sample angle
            way = (
                f"the way its angle {'increases' if grows else 'decreases'} "
                f"(as when a sweep moves it to a {'larger' if grows else 'smaller'} angle)"
            )
        state = "homed" if reading.homed else "NOT homed, so this angle may be wrong"
        typer.echo(
            f"  {STAGE_LABELS[stage]:<12} now at {hardware.arm_angle(reading, large, cfg.stages):.2f}° ({state}).\n"
            f"  {'':<12} Homing starts turning {way}."
        )
    typer.echo(
        "Check that the whole way round in those directions is free: cables, the micrometer and mounts on the "
        "sample holder, and the Tx and Rx modules. Keep a hand on STOP (or Ctrl+C)."
    )
    return _ask("Type 'clear' to home, anything else to cancel: ") == "clear"


def _read_for_homing(large: Any, small: Any) -> tuple[list[hardware.ArmReading], dict[str, int | None]]:
    readings = [hardware.read_arm(large), hardware.read_arm(small)]
    return readings, {RECEIVER: homing_direction(large), SAMPLE: homing_direction(small)}


class HomeStages(str, Enum):
    both = "both"
    receiver = RECEIVER
    sample = SAMPLE


HOME_STAGE_OPTION = typer.Option(
    HomeStages.both, "--stage", help="Which stage to home: both (default; the sample first), receiver, or sample."
)


@app.command()
def homing(
    stage: HomeStages = HOME_STAGE_OPTION,
    gui: bool = GUI_OPTION,
    simulate: bool = typer.Option(False, "--simulate", help="Home a simulated setup instead, to try the command."),
    skip_check: bool = typer.Option(
        False, "--skip-check", help="Start even if the stage check reports a problem. Only if the check itself is wrong."
    ),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Drive the stages to their home sensors and set their counts to 0 there, so their angles are
    right again (after the controllers were switched on, for example). Asks you to confirm the path
    is clear before anything moves, and shows where the arms are afterwards."""
    cfg = _load_config(config)
    to_home = list(HOMING_ORDER) if stage is HomeStages.both else [stage.value]
    rig = None
    if simulate:
        rig = simulation.SimRig(cfg)
        rig.large.homed = rig.small.homed = False
    try:
        with clock.accelerated(DEFAULT_SIM_SPEED if simulate else 1.0):
            if not simulate:
                _preflight(lambda: hardware.check_stages(cfg), cfg, skip_check, simulated=False)
            with _readable_stages(cfg, simulate, rig) as axes:
                readings, directions = _read_for_homing(*axes)
            if not _confirm_homing(to_home, readings, directions, cfg):
                typer.echo("Cancelled. Nothing was moved.")
                raise typer.Exit(code=1)

            if rig is not None:
                for axis in (rig.large, rig.small):
                    axis.open_device()
                large, small = rig.large, rig.small
            else:
                try:
                    large, small = open_stages(cfg.stages)
                except HardwareUnavailable as e:
                    typer.echo(f"Error: {e}")
                    raise typer.Exit(code=1) from None
            progress = HomingProgress(to_home)
            order = ", then ".join(STAGE_LABELS[s] for s in progress.stages)
            try:
                run_with_emergency_stop(
                    home_stages, [large, small], list(sweeps.STAGE_NAMES),
                    large, small, to_home, cfg.stages, progress.homed,
                    gui=gui, title=f"Homing {order}", window=homing_window(progress, simulate),
                )
            except StopWindowUnavailable as e:
                typer.echo(f"Error: {e}")
                raise typer.Exit(code=1) from None

            done = progress.done()
            missing = [STAGE_LABELS[s] for s in progress.stages if s not in done]
            if missing:
                typer.echo(f"Not homed: {', '.join(missing)}. Its count was left as it was.")
            else:
                typer.echo("Homing finished.")
            typer.echo(
                f"In the settings the receiver's home is {cfg.stages.zero_l:g}° and the sample's "
                f"{-cfg.stages.zero_s:g}° from Tx: check the arms are there on the table."
            )
            with _readable_stages(cfg, simulate, rig) as axes:
                reader = hardware.PositionReader(*axes).start()
                try:
                    if gui:
                        _position_window(
                            reader, cfg, config, simulated=simulate,
                            heading=(
                                f"After homing: the receiver's home is {cfg.stages.zero_l:g}°, the "
                                f"sample's {-cfg.stages.zero_s:g}°.\nDoes the drawing match the table now?"
                            ),
                        )
                    else:
                        typer.echo(hardware.format_readings(*reader.latest(), cfg.stages))
                finally:
                    reader.stop()
    finally:
        if rig is not None:
            rig.close()
    if missing:
        raise typer.Exit(code=1)


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


def _confirm_positions(
    cfg: cfgmod.AppConfig,
    config_path: Path | None,
    plan: list[sweeps.PlannedStep],
    title: str,
    gui: bool,
    set_zero: bool = False,
) -> tuple[tuple[hardware.ArmReading, hardware.ArmReading], cfgmod.AppConfig]:
    """Step 1 of 3 before a real sweep: where the arms are, read without
    moving them, and whether that matches the real setup. Returns the
    readings and the config with the drawing as it was left; exits if the
    person does not confirm."""
    typer.echo("Step 1 of 3: where the arms are now (read only, nothing moves).")
    with _readable_stages(cfg, simulated=False) as axes:
        reader = hardware.PositionReader(*axes).start()
        try:
            if gui:
                result = _position_window(
                    reader, cfg, config_path, plan=plan, confirm=True,
                    heading=(
                        f"Step 1 of 3 before: {title}\n"
                        + (
                            "With --set-zero the stages are homed first, so these angles may still be wrong. "
                            "Confirm to see the sweep simulated."
                            if set_zero
                            else "Does the drawing match the real arms?"
                        )
                    ),
                )
            else:
                readings, errors = reader.latest()
                typer.echo(hardware.format_readings(readings, errors, cfg.stages))
                usable = all(readings) and not any(errors)
                if not usable:
                    typer.echo("A stage could not be read, so where it is can't be confirmed.")
                answer = _ask("Do these match the real arms? Type 'yes' to simulate the sweep from here: ") if usable else ""
                result = PositionResult(answer == "yes", tuple(readings) if answer == "yes" else None, cfg.view)
        finally:
            reader.stop()
    if not result.confirmed or result.readings is None:
        typer.echo("Cancelled. Nothing was moved.")
        raise typer.Exit(code=1)
    for name, reading, large in (("Receiver", result.readings[0], True), ("Sample", result.readings[1], False)):
        typer.echo(f"  {name} confirmed at {hardware.arm_angle(reading, large, cfg.stages):.2f}°.")
    return result.readings, replace(cfg, view=result.view)


def _stop_if_moved(large_stage, small_stage, start: tuple[hardware.ArmReading, hardware.ArmReading]) -> None:
    """The simulation started where the arms were when they were confirmed.
    If a stage has moved since, it no longer shows what this sweep will do."""
    now = (large_stage.get_position_calb().Position, small_stage.get_position_calb().Position)
    moved = [
        f"{name} {before.position:.2f}° then, {after:.2f}° now (controller angles)"
        for name, before, after in zip(("receiver", "sample"), start, now)
        if abs(after - before.position) > POSITION_TOLERANCE
    ]
    if moved:
        for axis in (large_stage, small_stage):
            try:
                axis.close_device()
            except Exception:  # noqa: BLE001, S110 -- leaving anyway
                pass
        typer.echo(
            "Error: a stage moved after its position was confirmed (" + "; ".join(moved) + "), so the "
            "simulation no longer shows what this sweep will do. Nothing was moved; run the command again."
        )
        raise typer.Exit(code=1)


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
    skip_preview: bool,
    config_path: Path | None,
) -> None:
    if speed is not None and (speed <= 0 or (skip_preview and not simulate)):
        typer.echo(
            "Error: --speed sets how fast simulations run (a positive number): with --simulate, or the "
            "simulation before a real sweep, which --skip-preview leaves out."
        )
        raise typer.Exit(code=1)

    cfg = _load_config(config_path)
    freq_start = cfg.scan_defaults.freq_start if freq_start is None else freq_start
    freq_stop = cfg.scan_defaults.freq_stop if freq_stop is None else freq_stop
    int_time = cfg.scan_defaults.int_time if int_time is None else int_time
    speed = DEFAULT_SIM_SPEED if speed is None else speed

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
    sweep_args = (start_angle, end_angle, step, freq_start, freq_stop, int_time, filename, set_zero)

    if simulate:
        _simulate(sweep_fn, plan, title, cfg, sweep_args, gui, speed, skip_check)
        return

    checked = _preflight(lambda: hardware.check_hardware(cfg), cfg, skip_check, simulated=False)

    start = None
    if not skip_preview:
        start, cfg = _confirm_positions(cfg, config_path, plan, title, gui, set_zero)
        typer.echo("Step 2 of 3: the same sweep, simulated from where the arms are (nothing moves).")
        if not _simulate(sweep_fn, plan, title, cfg, sweep_args, gui, speed, skip_check, start=start):
            typer.echo("Cancelled. Nothing was moved.")
            raise typer.Exit(code=1)
        typer.echo("Step 3 of 3: the sweep on the real hardware.")
        toptica.link.reset()
        toptica.progress.reset()

    try:
        large_stage, small_stage = open_stages(cfg.stages)
    except HardwareUnavailable as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)
    if start is not None:
        _stop_if_moved(large_stage, small_stage, start)
    if set_zero:
        readings, directions = _read_for_homing(large_stage, small_stage)
        if not _confirm_homing(list(HOMING_ORDER), readings, directions, cfg):
            for axis in (large_stage, small_stage):
                try:
                    axis.close_device()
                except Exception:  # noqa: BLE001, S110 -- leaving anyway
                    pass
            typer.echo("Cancelled. Nothing was moved.")
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
            *sweep_args,
            overwrite,
            gui=gui,
            title=title,
            window=live_view.live_window(source, plan, files, cfg),
        )
    except (FileExistsError, StopWindowUnavailable) as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)


def _simulate(
    sweep_fn,
    plan: list[sweeps.PlannedStep],
    title: str,
    cfg: cfgmod.AppConfig,
    sweep_args: tuple,
    gui: bool,
    speed: float,
    skip_check: bool,
    start: tuple[hardware.ArmReading, hardware.ArmReading] | None = None,
) -> bool:
    """Runs the same sweep code against simulated stages and TOptica, and
    prints planned-vs-actual angles for every step afterwards.

    With `start` (readings of the real stages) it is the simulation before a
    real sweep: the simulated arms start where the real ones are, and at the
    end the person is asked whether to run the sweep on the real hardware.
    Returns that answer (False for a plain simulation)."""
    before_real = start is not None
    filename = sweep_args[6]
    if before_real:
        typer.echo(
            f"The simulated arms start where the real ones are; time runs x{speed:g}; "
            f"files go to {SIM_OUTPUT_DIR}/. Nothing moves yet."
        )
    else:
        typer.echo(
            f"SIMULATION: no hardware is used. Stages and TOptica are simulated, time runs x{speed:g}, "
            f"and the photocurrent is made up. Files go to {SIM_OUTPUT_DIR}/."
        )
    with clock.accelerated(speed):
        rig = simulation.SimRig(cfg, SIM_OUTPUT_DIR, start=start)
        sim_filename = str(SIM_OUTPUT_DIR / filename)
        freq_start, freq_stop, int_time = sweep_args[3:6]
        files = [s.output_file(sim_filename, int_time, freq_start, freq_stop) for s in plan]
        window = None
        t0 = time.monotonic()
        ran = False
        try:
            if before_real:  # the real hardware was checked already; these only feed the lights
                checked = hardware.check_simulated(rig)
            else:
                checked = _preflight(lambda: hardware.check_simulated(rig), cfg, skip_check, simulated=True)
            large, small = _watch(rig.large), _watch(rig.small)
            source = live_view.SweepSource(large, small, cfg.stages, checked, simulated=True)
            window = live_view.live_window(
                source, plan, files, cfg, verdict=(lambda: rig.verdict(plan)) if before_real else None
            )
            run_with_emergency_stop(
                sweep_fn,
                [large, small],
                list(sweeps.STAGE_NAMES),
                large,
                small,
                rig.cfg,
                *sweep_args[:6],
                sim_filename,
                sweep_args[7],
                True,  # replace files from earlier simulations
                gui=gui,
                title=f"{title} (simulated)",
                window=window,
            )
            ran = True
        except StopWindowUnavailable as e:
            typer.echo(f"Error: {e}")
            raise typer.Exit(code=1)
        except Exception as e:
            if not before_real:
                raise
            typer.echo(f"\nThe simulated sweep failed: {type(e).__name__}: {e}")
            typer.echo("The real sweep would fail the same way.")
        finally:
            rig.close()
            if ran or rig.toptica.scans or rig.large.moves or rig.small.moves:
                typer.echo(rig.summary(plan, [Path(f) for f in files], time.monotonic() - t0))
    if not before_real or not ran:
        return False
    if gui:
        return bool(window and window.decision)
    ok, lines = rig.verdict(plan)
    typer.echo("\n".join(lines))
    if not ok:
        return False
    return _ask("Type 'run' to send this sweep to the real hardware, anything else to cancel: ") == "run"


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
    skip_preview: bool = SKIP_PREVIEW_OPTION,
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a specular (theta-2theta) reflectometry sweep."""
    title = f"Specular sweep: sample {start_angle}° to {end_angle}° in {step}° steps"
    _run(
        sweeps.sweep_spec, sweeps.plan_spec, title, start_angle, end_angle, step, freq_start, freq_stop,
        int_time, filename, set_zero, overwrite, gui, simulate, speed, skip_check, skip_preview, config,
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
    skip_preview: bool = SKIP_PREVIEW_OPTION,
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Run a non-specular reflectometry sweep (receiver stage only)."""
    title = f"Non-specular sweep: receiver {start_angle}° to {end_angle}° in {step}° steps"
    _run(
        sweeps.sweep_nonspec, sweeps.plan_nonspec, title, start_angle, end_angle, step, freq_start, freq_stop,
        int_time, filename, set_zero, overwrite, gui, simulate, speed, skip_check, skip_preview, config,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
