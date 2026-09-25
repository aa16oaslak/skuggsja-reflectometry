import threading

import pytest

from hi_skuggsja_reflectometry import toptica
from hi_skuggsja_reflectometry.config import TopticaConfig


def make_toptica_config(**overrides) -> TopticaConfig:
    defaults = {
        "host": "127.0.0.1",
        "port": 1998,
        "freq_step": 0.05,
        "settle_freq_tol": 0.1,
        "settle_timeout": 10.0,
        "amp_tol": 0.0005,
        "offset_tol": 0.0002,
        "gain_default": 330000,
        "stabilize_tol": 0.01,
        "stabilize_hold": 2.0,
        "stabilize_timeout": 30.0,
    }
    defaults.update(overrides)
    return TopticaConfig(**defaults)


def test_build_output_filename():
    assert toptica.build_output_filename("run_30degrees", 3, 70, 400) == "run_30degrees_3ms_70GHz_to_400.txt"


def test_scan_refuses_to_overwrite_existing_file_before_connecting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / toptica.build_output_filename("run", 3, 70, 400)
    existing.write_text("previous data\n")

    def boom(*_args, **_kwargs):
        raise AssertionError("scan() should refuse before ever touching the network")

    monkeypatch.setattr(toptica.socket, "socket", boom)

    with pytest.raises(FileExistsError):
        toptica.scan(70, 400, 3, "run", threading.Event(), make_toptica_config(), overwrite=False)


def test_scan_overwrite_true_gets_past_the_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / toptica.build_output_filename("run", 3, 70, 400)
    existing.write_text("previous data\n")

    reached_network = {"value": False}

    def fake_socket_ctor():
        reached_network["value"] = True
        raise RuntimeError("stop here, no real network in this test")

    monkeypatch.setattr(toptica.socket, "socket", fake_socket_ctor)

    with pytest.raises(RuntimeError, match="stop here"):
        toptica.scan(70, 400, 3, "run", threading.Event(), make_toptica_config(), overwrite=True)

    assert reached_network["value"] is True
