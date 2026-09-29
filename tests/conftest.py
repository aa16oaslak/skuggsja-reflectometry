import pytest

from hi_skuggsja_reflectometry import clock, simulation, toptica
from hi_skuggsja_reflectometry import config as cfgmod

FAST = 1000  # clock speed for simulated runs in tests


@pytest.fixture(autouse=True)
def fresh_toptica_status():
    """toptica.progress and toptica.link are module-wide; start each test clean."""
    toptica.progress.reset()
    toptica.link.reset()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return cfgmod.load_config()


@pytest.fixture
def rig(cfg, tmp_path):
    """A simulated setup (both stages + TOptica) with the default config."""
    with clock.accelerated(FAST):
        rig = simulation.SimRig(cfg, tmp_path / "sim")
        yield rig
        rig.close()
