import threading

import pytest

from hi_skuggsja_reflectometry import config as cfgmod
from hi_skuggsja_reflectometry import sweeps
from hi_skuggsja_reflectometry.toptica import build_output_filename
from tests.fakes import FakeAxis


def test_check_outputs_available_raises_on_collision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    colliding = build_output_filename("run_45.0degrees", 3, 70, 400)
    (tmp_path / colliding).write_text("previous data\n")

    with pytest.raises(FileExistsError):
        sweeps._check_outputs_available("run", 30.0, 15.0, 1, 70, 400, 3, overwrite=False)


def test_check_outputs_available_passes_when_nothing_collides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sweeps._check_outputs_available("run", 30.0, 15.0, 1, 70, 400, 3, overwrite=False)  # no raise


def test_check_outputs_available_overwrite_bypasses_collision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    colliding = build_output_filename("run_45.0degrees", 3, 70, 400)
    (tmp_path / colliding).write_text("previous data\n")

    sweeps._check_outputs_available("run", 30.0, 15.0, 1, 70, 400, 3, overwrite=True)  # no raise


@pytest.fixture
def default_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return cfgmod.load_config()


def test_sweep_nonspec_moves_large_stage_and_scans_each_step(monkeypatch, default_config):
    large, small = FakeAxis(), FakeAxis()
    scan_calls = []
    monkeypatch.setattr(sweeps, "scan", lambda *args: scan_calls.append(args))

    sweeps.sweep_nonspec(
        large, small, default_config,
        start=30.0, end=60.0, step=15.0,
        freq_start=70, freq_stop=400, int_time=3,
        filename="run", setzero=False, overwrite=True,
        stop_event=threading.Event(),
    )

    # one scan per angle step: 30, 45, 60 (floats, matching real CLI option types)
    assert [call[3] for call in scan_calls] == ["run_30.0degrees", "run_45.0degrees", "run_60.0degrees"]
    # small stage never moves after the initial parking position in nonspec mode
    assert not any(call[0] == "movr_calb" for call in small.calls)


def test_sweep_spec_moves_both_stages_at_double_ratio(monkeypatch, default_config):
    large, small = FakeAxis(), FakeAxis()
    scan_calls = []
    monkeypatch.setattr(sweeps, "scan", lambda *args: scan_calls.append(args))

    sweeps.sweep_spec(
        large, small, default_config,
        start=15.0, end=30.0, step=7.5,
        freq_start=70, freq_stop=400, int_time=3,
        filename="run", setzero=False, overwrite=True,
        stop_event=threading.Event(),
    )

    assert [call[3] for call in scan_calls] == ["run_15.0degrees", "run_22.5degrees", "run_30.0degrees"]
    # large-stage relative moves are negated (per rotate_relative's large-axis
    # convention) and exactly double the small stage's, matching theta-2theta
    large_moves = [delta for kind, delta in large.calls if kind == "movr_calb"]
    small_moves = [delta for kind, delta in small.calls if kind == "movr_calb"]
    assert large_moves == [-2 * d for d in small_moves]


def test_sweep_nonspec_stops_after_emergency_stop_mid_run(monkeypatch, default_config):
    large, small = FakeAxis(), FakeAxis()
    stop_event = threading.Event()
    scan_calls = []

    def fake_scan(*args):
        scan_calls.append(args)
        stop_event.set()  # simulate ESC pressed during the first scan

    monkeypatch.setattr(sweeps, "scan", fake_scan)

    sweeps.sweep_nonspec(
        large, small, default_config,
        start=30, end=60, step=15,
        freq_start=70, freq_stop=400, int_time=3,
        filename="run", setzero=False, overwrite=True,
        stop_event=stop_event,
    )

    assert len(scan_calls) == 1  # sweep returned instead of continuing to the next angle
