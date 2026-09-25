from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .config import AppConfig
from .stages import home_and_zero, position, rotate_relative, rotate_to_angle
from .toptica import build_output_filename, scan

if TYPE_CHECKING:
    import libximc.highlevel as ximc

STAGE_NAMES = ("8MRB450", "8MR174")
NONSPEC_SAMPLE_ANGLE = 15  # degrees; where the sample sits during a non-specular sweep


class PlannedStep(NamedTuple):
    """One angle of a sweep: where both stages should be for its scan."""

    sample: float
    receiver: float
    file_angle: float  # the angle in this step's output filename

    def output_file(self, filename: str, int_time: float, freq_start: float, freq_stop: float) -> str:
        return build_output_filename(f"{filename}_{self.file_angle}degrees", int_time, freq_start, freq_stop)


def plan_spec(start: float, end: float, step: float) -> list[PlannedStep]:
    """The steps a specular sweep scans at, in order (same arithmetic as
    sweep_spec)."""
    amount = int((end - start) / step)
    steps = []
    for i in range(amount + 1):
        n = start + i * step
        steps.append(PlannedStep(n, 2 * start + i * (2 * step), n))
    return steps


def plan_nonspec(start: float, end: float, step: float) -> list[PlannedStep]:
    """The steps a non-specular sweep scans at, in order (same arithmetic
    as sweep_nonspec)."""
    amount = int((end - start) / step)
    steps = []
    for i in range(amount + 1):
        n = start + i * step
        steps.append(PlannedStep(NONSPEC_SAMPLE_ANGLE, n, n))
    return steps


def _check_outputs_available(
    filename: str,
    start: float,
    step: float,
    amount: int,
    freq_start: float,
    freq_stop: float,
    int_time: float,
    overwrite: bool,
) -> None:
    """Refuses to start the sweep if any of its output files already exist.
    Checked before any stage motion so a naming collision never wastes a
    scan or leaves the stages mid-move."""
    if overwrite:
        return
    for i in range(amount + 1):
        n = start + i * step
        path = build_output_filename(f"{filename}_{n}degrees", int_time, freq_start, freq_stop)
        if Path(path).exists():
            raise FileExistsError(
                f"Output file already exists: {path} (pass --overwrite to allow overwriting)"
            )


def check_outputs_available(
    filename: str,
    start: float,
    end: float,
    step: float,
    freq_start: float,
    freq_stop: float,
    int_time: float,
    overwrite: bool,
) -> None:
    """Same check the sweeps run first, for callers that want to fail before
    opening any hardware."""
    amount = int((end - start) / step)
    _check_outputs_available(filename, start, step, amount, freq_start, freq_stop, int_time, overwrite)


def sweep_nonspec(
    large_stage: ximc.Axis,
    small_stage: ximc.Axis,
    cfg: AppConfig,
    start: float,
    end: float,
    step: float,
    freq_start: float,
    freq_stop: float,
    int_time: float,
    filename: str,
    setzero: bool,
    overwrite: bool,
    stop_event: threading.Event,
) -> None:
    """Non-specular sweep: only the receiver (large) stage moves between
    frequency scans; the sample (small) stage stays fixed."""
    zero_l, zero_s = cfg.stages.zero_l, cfg.stages.zero_s
    angle_min, angle_max = cfg.stages.angle_min, cfg.stages.angle_max
    amount = int((end - start) / step)

    _check_outputs_available(filename, start, step, amount, freq_start, freq_stop, int_time, overwrite)

    if setzero:
        home_and_zero(
            large_stage, small_stage, STAGE_NAMES[0], STAGE_NAMES[1],
            angle_min, angle_max, cfg.stages.res_large, zero_l, zero_s, stop_event,
        )
    else:
        print("Rotating receiver to 30°")
        print(f"Rotating sample to {NONSPEC_SAMPLE_ANGLE}°")
        rotate_to_angle(large_stage, 30, True, stop_event, zero_l, zero_s, angle_min, angle_max)
        if stop_event.is_set():
            return
        rotate_to_angle(small_stage, NONSPEC_SAMPLE_ANGLE, False, stop_event, zero_l, zero_s, angle_min, angle_max)
        if stop_event.is_set():
            return
        print(f"Rotated receiver to {position(large_stage, True, zero_l, zero_s)}°")
        print(f"Rotated sample to {position(small_stage, False, zero_l, zero_s)}°")

    if stop_event.is_set():
        return

    print(f"Rotating receiver to {start}°")
    rotate_to_angle(large_stage, start, True, stop_event, zero_l, zero_s, angle_min, angle_max)
    if stop_event.is_set():
        return
    print(f"Rotated receiver to start position: {position(large_stage, True, zero_l, zero_s)}°")

    if stop_event.is_set():
        return

    for i in range(amount + 1):
        if stop_event.is_set():
            print("🛑 Sweep interrupted by emergency stop")
            return

        if i != 0:
            print(f"Rotating receiver to {start + i * step}°")
            rotate_relative(large_stage, step, True, stop_event, zero_l, angle_min, angle_max)
            if stop_event.is_set():
                return
            print(f"Rotated receiver to {position(large_stage, True, zero_l, zero_s)}°.")

        if stop_event.is_set():
            return

        print("Connecting to Toptica and starting scan")
        n = start + i * step
        scan(freq_start, freq_stop, int_time, f"{filename}_{n}degrees", stop_event, cfg.toptica, overwrite)
        if stop_event.is_set():
            return


def sweep_spec(
    large_stage: ximc.Axis,
    small_stage: ximc.Axis,
    cfg: AppConfig,
    start: float,
    end: float,
    step: float,
    freq_start: float,
    freq_stop: float,
    int_time: float,
    filename: str,
    setzero: bool,
    overwrite: bool,
    stop_event: threading.Event,
) -> None:
    """Specular (theta-2theta) sweep: the receiver (large) stage moves at
    twice the angle of the sample (small) stage on every step."""
    zero_l, zero_s = cfg.stages.zero_l, cfg.stages.zero_s
    angle_min, angle_max = cfg.stages.angle_min, cfg.stages.angle_max
    amount = int((end - start) / step)

    _check_outputs_available(filename, start, step, amount, freq_start, freq_stop, int_time, overwrite)

    if setzero:
        home_and_zero(
            large_stage, small_stage, STAGE_NAMES[0], STAGE_NAMES[1],
            angle_min, angle_max, cfg.stages.res_large, zero_l, zero_s, stop_event,
        )
    else:
        print("Rotating receiver to 30°")
        print("Rotating sample to 0°")
        rotate_to_angle(large_stage, 30, True, stop_event, zero_l, zero_s, angle_min, angle_max)
        if stop_event.is_set():
            return
        rotate_to_angle(small_stage, 0, False, stop_event, zero_l, zero_s, angle_min, angle_max)
        if stop_event.is_set():
            return
        print(f"Rotated receiver to {position(large_stage, True, zero_l, zero_s)}°")
        print(f"Rotated sample to {position(small_stage, False, zero_l, zero_s)}°")

    if stop_event.is_set():
        return

    print(f"Rotating receiver to {2 * start}°")
    print(f"Rotating sample to {start}°")
    rotate_to_angle(large_stage, 2 * start, True, stop_event, zero_l, zero_s, angle_min, angle_max)
    if stop_event.is_set():
        return
    rotate_to_angle(small_stage, start, False, stop_event, zero_l, zero_s, angle_min, angle_max)
    if stop_event.is_set():
        return
    print(f"Rotated receiver to start position: {position(large_stage, True, zero_l, zero_s)}°")
    print(f"Rotated sample to start position: {position(small_stage, False, zero_l, zero_s)}°")

    if stop_event.is_set():
        return

    step_l = 2 * step

    for i in range(amount + 1):
        if stop_event.is_set():
            print("🛑 Sweep interrupted by emergency stop")
            return

        if i != 0:
            print(f"Rotating receiver to {2 * start + i * step_l}°")
            print(f"Rotating sample to {start + i * step}°")

            rotate_relative(large_stage, step_l, True, stop_event, zero_l, angle_min, angle_max)
            if stop_event.is_set():
                return
            rotate_relative(small_stage, step, False, stop_event, zero_l, angle_min, angle_max)
            if stop_event.is_set():
                return

            print(f"Rotated receiver to {position(large_stage, True, zero_l, zero_s)}°.")
            print(f"Rotated sample to {position(small_stage, False, zero_l, zero_s)}°.")

        if stop_event.is_set():
            return

        print("Connecting to Toptica and starting scan")
        n = start + i * step
        scan(freq_start, freq_stop, int_time, f"{filename}_{n}degrees", stop_event, cfg.toptica, overwrite)
        if stop_event.is_set():
            return
