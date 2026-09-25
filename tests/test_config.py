from hi_skuggsja_reflectometry import config as cfgmod


def test_load_config_defaults_with_no_local_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = cfgmod.load_config()

    assert cfg.stages.zero_l == 180.5
    assert cfg.stages.zero_s == -40
    assert cfg.stages.angle_min == 30
    assert cfg.toptica.host == "192.0.2.1"
    assert cfg.toptica.port == 1998
    assert cfg.scan_defaults.freq_stop == 400


def test_load_config_auto_discovers_local_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reflecto.toml").write_text(
        '[stages]\ndevice_uri_large = "xi-com:\\\\.\\\\COM9"\n'
    )

    cfg = cfgmod.load_config()

    assert cfg.stages.device_uri_large == "xi-com:\\.\\COM9"
    # untouched keys still fall back to the packaged default
    assert cfg.stages.device_uri_small == "xi-com:\\\\.\\COM4"
    assert cfg.stages.zero_l == 180.5


def test_load_config_explicit_path_overrides_one_key(tmp_path):
    override = tmp_path / "custom.toml"
    override.write_text("[toptica]\nhost = \"10.0.0.5\"\n")

    cfg = cfgmod.load_config(override)

    assert cfg.toptica.host == "10.0.0.5"
    assert cfg.toptica.port == 1998  # falls back to default
    assert cfg.scan_defaults.freq_start == 70


def test_write_default_config_round_trips(tmp_path):
    dest = tmp_path / "written.toml"

    cfgmod.write_default_config(dest)

    assert dest.exists()
    cfg = cfgmod.load_config(dest)
    assert cfg.stages.zero_l == 180.5
    assert cfg.toptica.host == "192.0.2.1"
