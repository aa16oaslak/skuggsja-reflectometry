import socket
import sys
import threading

import libximc.highlevel as real_ximc
import pytest

from hi_skuggsja_reflectometry import clock, hardware, live_view, simulation, toptica
from hi_skuggsja_reflectometry import config as cfgmod
from hi_skuggsja_reflectometry.hardware import FAIL, OK, WARN, WatchedAxis
from tests.conftest import FAST
from tests.fakes import FakeAxis


def toptica_cfg(cfg, port, host="127.0.0.1"):
    return cfgmod.TopticaConfig(**{**vars(cfg.toptica), "host": host, "port": port})


def serve_once(handler):
    """A one-connection TCP server on localhost; returns its port."""
    server = socket.create_server(("127.0.0.1", 0))

    def run():
        conn, _ = server.accept()
        with conn:
            handler(conn)
        server.close()

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


# -- TOptica ---------------------------------------------------------------------------


def test_toptica_ready(cfg):
    sim = simulation.SimToptica(cfg.toptica, lambda: (0.0, 0.0))
    try:
        status = hardware.probe_toptica(toptica_cfg(cfg, sim.port))
    finally:
        sim.close()
    assert status.state == OK
    assert "ready" in status.summary
    assert sim.scans == []  # a check is not a scan


def test_toptica_placeholder_address_is_not_contacted(cfg):
    status = hardware.probe_toptica(cfg.toptica)  # the packaged default, 192.0.2.1
    assert status.state == FAIL
    assert "no instrument address configured" in status.summary


def test_toptica_not_answering(cfg):
    unused = socket.create_server(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    status = hardware.probe_toptica(toptica_cfg(cfg, port), timeout=1.0)
    assert status.state == FAIL
    assert "no answer" in status.summary


def test_toptica_hanging_up_is_reported_instead_of_hanging_the_check(cfg):
    port = serve_once(lambda conn: None)  # accepts, then closes without a prompt
    status = hardware.probe_toptica(toptica_cfg(cfg, port), timeout=1.0)
    assert status.state == FAIL
    assert "did not answer as expected" in status.summary


def test_toptica_with_lockin_settings_a_scan_would_refuse(cfg):
    class Misconfigured(simulation.SimToptica):
        def _answer(self, cmd, link):
            return "0.7" if "mod-out-amplitude-default" in cmd else super()._answer(cmd, link)

    sim = Misconfigured(cfg.toptica, lambda: (0.0, 0.0))
    try:
        status = hardware.probe_toptica(toptica_cfg(cfg, sim.port))
    finally:
        sim.close()
    assert status.state == FAIL
    assert "a scan would refuse to start" in status.summary
    assert any("mod_out_amplitude" in d for d in status.details)


def test_read_until_prompt_raises_when_the_instrument_hangs_up():
    a, b = socket.socketpair()
    b.close()
    with a, pytest.raises(ConnectionError):
        toptica.read_until_prompt(a)


def test_scan_reports_progress_and_link(cfg, tmp_path):
    sim = simulation.SimToptica(cfg.toptica, lambda: (20.0, 40.0))
    try:
        with clock.accelerated(FAST):
            toptica.scan(70, 70.5, 3, str(tmp_path / "run"), threading.Event(), toptica_cfg(cfg, sim.port))
    finally:
        sim.close()
    snap = toptica.progress.snapshot()
    assert (snap.started, snap.active, snap.points) == (1, False, snap.total)
    assert snap.total == len(toptica.frequency_grid(70, 70.5, cfg.toptica.freq_step))
    last_ok, error = toptica.link.snapshot()
    assert last_ok is not None and error is None


# -- stages ------------------------------------------------------------------------------


def fake_ximc(monkeypatch, axis_factory):
    # stages._load_ximc() imports and returns the real module, so patch its Axis
    monkeypatch.setattr(real_ximc, "Axis", axis_factory)


def test_stage_ready_and_closed_again(cfg, monkeypatch):
    axis = FakeAxis(position=cfg.stages.zero_l - 60)
    fake_ximc(monkeypatch, lambda uri: axis)

    status = hardware.probe_stage(hardware.RECEIVER, cfg.stages.device_uri_large, True, cfg.stages)

    assert status.state == OK
    assert status.address == "COM3"
    assert status.summary == "at 60.00°, ready"
    assert axis.calls[-1] == ("close",)
    assert not any(c[0] in ("move_calb", "movr_calb", "homezero") for c in axis.calls)  # nothing moved


@pytest.mark.parametrize(
    "flags, state, text",
    [
        (0x20 | 0x40, FAIL, "ALARM"),
        (0x20 | 0x10000, FAIL, "motor power supply"),
        (0x0, WARN, "not homed"),
    ],
)
def test_stage_flags(cfg, monkeypatch, flags, state, text):
    class Flagged(FakeAxis):
        def get_status(self):
            status = super().get_status()
            status.Flags = flags
            return status

    fake_ximc(monkeypatch, lambda uri: Flagged(position=cfg.stages.zero_l - 60))
    status = hardware.probe_stage(hardware.RECEIVER, cfg.stages.device_uri_large, True, cfg.stages)
    assert status.state == state
    assert text in status.summary


def test_stage_that_cannot_be_opened(cfg, monkeypatch):
    class Unplugged(FakeAxis):
        def open_device(self):
            raise ConnectionError("Cannot connect to device\n\t* check URI")

    fake_ximc(monkeypatch, lambda uri: Unplugged())
    status = hardware.probe_stage(hardware.SAMPLE, cfg.stages.device_uri_small, False, cfg.stages)
    assert status.state == FAIL
    assert "cannot open COM4" in status.summary
    assert status.details == [f"{cfg.stages.device_uri_small}: Cannot connect to device"]  # first line only


def test_stage_without_the_standa_driver(cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "libximc.highlevel", None)
    status = hardware.probe_stage(hardware.RECEIVER, cfg.stages.device_uri_large, True, cfg.stages)
    assert status.state == FAIL
    assert "libximc" in status.summary


def test_simulated_setup_checks_ok(rig):
    statuses = hardware.check_simulated(rig)
    assert [s.state for s in statuses] == [OK, OK, OK]
    assert {s.address for s in statuses} == {"simulated"}
    assert hardware.all_usable(statuses)
    table = hardware.format_statuses(statuses)
    assert table.count("[ ok ]") == 3


def test_warnings_do_not_block_but_failures_do():
    ok = hardware.DeviceStatus("a", "x", OK, "")
    assert hardware.all_usable([ok, hardware.DeviceStatus("b", "x", WARN, "")])
    assert not hardware.all_usable([ok, hardware.DeviceStatus("b", "x", FAIL, "")])


# -- watching a running sweep ----------------------------------------------------------------


def test_watched_axis_follows_position_from_status_replies(rig):
    with clock.accelerated(FAST):
        watched = WatchedAxis(rig.large)
        start = watched.inner.position
        watched.command_move_calb(rig.zero_l - 45.0)
        seen = []
        while watched.get_status() and watched.moving:
            seen.append(watched.position)
            clock.sleep(0.1)
    assert len(seen) > 3 and seen == sorted(seen) and start <= seen[0]  # followed the move as it went
    assert rig.zero_l - watched.position == pytest.approx(45.0, abs=1e-4)
    assert watched.position == pytest.approx(rig.large.position, abs=1e-4)
    assert watched.link.snapshot()[1] is None


def test_watched_axis_records_a_failed_call_and_re_raises(rig):
    watched = WatchedAxis(rig.small)
    rig.small.close_device()
    with pytest.raises(RuntimeError, match="not open"):
        watched.get_status()
    assert "not open" in watched.link.snapshot()[1]


def test_live_status_falls_back_to_the_check_then_follows_the_sweep(rig):
    checked = hardware.check_simulated(rig)
    toptica.link.reset()
    large, small = WatchedAxis(rig.large), WatchedAxis(rig.small)
    source = live_view.SweepSource(large, small, rig.cfg.stages, checked, simulated=True)

    assert source.devices() == checked  # nothing said yet during the sweep

    large.get_status()
    toptica.link.ok()
    toptica.progress.begin(10)
    toptica.progress.at(71.25)
    receiver, sample, instrument = source.devices()
    assert receiver.state == OK and receiver.summary.startswith("idle")
    assert sample == checked[1]
    assert instrument.summary == "scanning, 71.25 GHz"

    toptica.link.failed(ConnectionResetError("reset by peer"))
    assert source.devices()[2].state == FAIL


# -- where the arms are, read without moving them --------------------------------------

MOVING_OR_WRITING = ("move_calb", "movr_calb", "homezero", "stop")


def test_open_stages_readonly_reads_and_closes_but_writes_nothing(cfg, monkeypatch):
    axes = {cfg.stages.device_uri_large: FakeAxis(position=2.4), cfg.stages.device_uri_small: FakeAxis(position=188.73)}
    fake_ximc(monkeypatch, lambda uri: axes[uri])
    before_edges = [axis.edges for axis in axes.values()]

    with hardware.open_stages_readonly(cfg.stages) as (large, small):
        reader = hardware.PositionReader(large, small)
        reader.read_now()
        readings, errors = reader.latest()

    assert [r.position for r in readings] == [2.4, 188.73]
    assert errors == [None, None]
    assert large.calibration == (cfg.stages.res_large, 9) and small.calibration == (cfg.stages.res_small, 9)
    for axis, edges in zip(axes.values(), before_edges):
        assert axis.calls[-1] == ("close",)
        assert not any(call[0] in MOVING_OR_WRITING for call in axis.calls)
        assert axis.edges is edges  # soft limits untouched


def test_open_stages_readonly_closes_the_first_stage_if_the_second_fails(cfg, monkeypatch):
    large = FakeAxis()

    class Missing(FakeAxis):
        def open_device(self):
            raise ConnectionError("no such port")

    fake_ximc(monkeypatch, lambda uri: large if uri == cfg.stages.device_uri_large else Missing())

    with pytest.raises(hardware.HardwareUnavailable, match="small \\(sample\\)"), hardware.open_stages_readonly(cfg.stages):
        pass

    assert large.calls[-1] == ("close",)


def test_reader_follows_the_arms_and_their_speeds(rig):
    reader = hardware.PositionReader(rig.large, rig.small, period=0.01).start()
    try:
        with clock.accelerated(FAST):
            rig.large.command_move_calb(rig.zero_l - 45)
            while rig.large.moving:
                clock.sleep(0.05)
        reader.read_now()
        (receiver, sample), errors = reader.latest()
    finally:
        reader.stop()

    assert errors == [None, None]
    assert hardware.arm_angle(receiver, True, rig.cfg.stages) == pytest.approx(45)
    assert hardware.arm_angle(sample, False, rig.cfg.stages) == pytest.approx(simulation.START_SAMPLE_ANGLE)
    assert (receiver.homed, receiver.moving) == (True, False)
    assert (receiver.speed, sample.speed) == (simulation.RECEIVER_SPEED, simulation.SAMPLE_SPEED)


def test_reader_keeps_the_last_good_reading_and_names_the_failure(rig):
    reader = hardware.PositionReader(rig.large, rig.small)
    reader.read_now()
    rig.large.close_device()  # as if the cable were pulled

    reader.read_now()
    readings, errors = reader.latest()

    assert readings[0] is not None
    assert "not open" in errors[0] and errors[1] is None


def test_format_readings_warns_about_stages_that_are_not_homed(cfg):
    readings = [hardware.ArmReading(2.4, homed=False, moving=False), hardware.ArmReading(188.73, True, False)]

    text = hardware.format_readings(readings, [None, None], cfg.stages)

    assert "178.10°" in text and "228.73°" in text
    assert "NOT homed" in text and "--set-zero" in text
