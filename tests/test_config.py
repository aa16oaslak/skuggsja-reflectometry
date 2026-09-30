import pytest

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


def test_view_defaults_to_the_papers_figure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    view = cfgmod.load_config().view
    assert (view.receiver_turns, view.tx_direction) == ("clockwise", 0.0)


def test_view_can_be_set_locally_and_directions_wrap_around(tmp_path):
    override = tmp_path / "custom.toml"
    override.write_text('[view]\nreceiver_turns = "counterclockwise"\ntx_direction = -45\n')

    view = cfgmod.load_config(override).view

    assert (view.receiver_turns, view.tx_direction) == ("counterclockwise", 315.0)


def test_receiver_turns_rejects_anything_else(tmp_path):
    override = tmp_path / "custom.toml"
    override.write_text('[view]\nreceiver_turns = "left"\n')

    with pytest.raises(cfgmod.ConfigError, match="receiver_turns"):
        cfgmod.load_config(override)


def test_old_receiver_turns_under_stages_says_where_it_went(tmp_path):
    override = tmp_path / "custom.toml"
    override.write_text('[stages]\nreceiver_turns = "counterclockwise"\n')

    with pytest.raises(cfgmod.ConfigError, match="moved from \\[stages\\] to \\[view\\]"):
        cfgmod.load_config(override)


@pytest.mark.parametrize(
    "text, message",
    [
        ("[stages]\nzero_L = 180.5\n", "unknown setting zero_L under \\[stages\\]"),
        ("[stage]\nzero_l = 180.5\n", "unknown section \\[stage\\]"),
        ("[stages\nzero_l = 180.5\n", "not valid TOML"),
    ],
)
def test_mistakes_in_the_settings_file_are_named(tmp_path, text, message):
    # a mistyped key used to be ignored silently, falling back to the default
    override = tmp_path / "custom.toml"
    override.write_text(text)

    with pytest.raises(cfgmod.ConfigError, match=message):
        cfgmod.load_config(override)


def test_missing_explicit_settings_file_is_named(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="not found"):
        cfgmod.load_config(tmp_path / "nope.toml")


VIEW = cfgmod.ViewConfig(receiver_turns="counterclockwise", tx_direction=135)


def test_save_view_creates_the_file_if_needed(tmp_path):
    path = tmp_path / "reflecto.toml"

    cfgmod.save_view(path, VIEW)

    assert cfgmod.load_config(path).view == VIEW


def test_save_view_adds_a_section_and_leaves_everything_else_alone(tmp_path):
    path = tmp_path / "reflecto.toml"
    original = '# lab settings\n[stages]\nzero_l = 181.0   # measured 2026-09-29\n\n[toptica]\nhost = "10.0.0.5"\n'
    path.write_text(original)

    cfgmod.save_view(path, VIEW)

    text = path.read_text()
    assert text.startswith(original)
    cfg = cfgmod.load_config(path)
    assert (cfg.view, cfg.stages.zero_l, cfg.toptica.host) == (VIEW, 181.0, "10.0.0.5")


def test_save_view_replaces_existing_values_in_place(tmp_path):
    path = tmp_path / "reflecto.toml"
    path.write_text('[view]\nreceiver_turns = "clockwise"  # old\n\n[toptica]\nport = 1999\n')

    cfgmod.save_view(path, VIEW)
    cfgmod.save_view(path, VIEW)  # saving twice changes nothing more

    text = path.read_text()
    assert text.count("receiver_turns") == 1 and text.count("tx_direction") == 1
    assert text.index("tx_direction") < text.index("[toptica]")  # added inside [view], not after [toptica]
    cfg = cfgmod.load_config(path)
    assert (cfg.view, cfg.toptica.port) == (VIEW, 1999)


def test_save_view_refuses_to_touch_a_broken_file(tmp_path):
    path = tmp_path / "reflecto.toml"
    path.write_text("[stages\n")

    with pytest.raises(cfgmod.ConfigError, match="not changed"):
        cfgmod.save_view(path, VIEW)

    assert path.read_text() == "[stages\n"
