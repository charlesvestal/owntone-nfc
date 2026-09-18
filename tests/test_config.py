# tests/test_config.py
import logging
from pathlib import Path

from nfc_jukebox.config import Config


def test_defaults_when_file_missing(tmp_path):
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.owntone_url == "http://127.0.0.1:3689"
    assert cfg.library_root == Path("/srv/music")
    assert cfg.grace_period_s == Config().grace_period_s


def test_reads_values_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "owntone_url: http://pi:3689\n"
        "library_root: /mnt/music\n"
        "bump_window_s: 0.4\n"
    )
    cfg = Config.load(path)
    assert cfg.owntone_url == "http://pi:3689"
    assert cfg.library_root == Path("/mnt/music")
    assert cfg.bump_window_s == 0.4


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("owntone_url: http://pi:3689\nnonsense: 1\n")
    cfg = Config.load(path)
    assert cfg.owntone_url == "http://pi:3689"


def test_invalid_yaml_falls_back_to_defaults(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("owntone_url: [unclosed\n")
    with caplog.at_level(logging.ERROR):
        cfg = Config.load(path)
    assert cfg.owntone_url == "http://127.0.0.1:3689"
    assert cfg.web_port == 8080
    assert "config.yaml" in caplog.text


def test_top_level_list_falls_back_to_defaults(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("- one\n- two\n")
    with caplog.at_level(logging.ERROR):
        cfg = Config.load(path)
    assert cfg.owntone_url == "http://127.0.0.1:3689"


def test_top_level_scalar_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("nonsense\n")
    assert Config.load(path).web_port == 8080


def test_empty_file_uses_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert Config.load(path).grace_period_s == 90.0


def test_unreadable_file_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("web_port: 9000\n")

    def boom(*args, **kwargs):
        raise PermissionError(13, "nope")

    monkeypatch.setattr(Path, "read_text", boom)
    assert Config.load(path).web_port == 8080


def test_non_numeric_float_field_falls_back_to_its_default(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("grace_period_s: ninety\nowntone_url: http://pi:3689\n")
    with caplog.at_level(logging.WARNING):
        cfg = Config.load(path)
    # A bad field costs that field, not the whole file.
    assert cfg.grace_period_s == Config().grace_period_s
    assert isinstance(cfg.grace_period_s, float)
    assert cfg.owntone_url == "http://pi:3689"
    assert "grace_period_s" in caplog.text


def test_numeric_strings_are_coerced(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('web_port: "8080"\nbump_window_s: "0.4"\ngrace_period_s: "30"\n')
    cfg = Config.load(path)
    assert cfg.web_port == 8080
    assert isinstance(cfg.web_port, int)
    assert cfg.bump_window_s == 0.4
    assert isinstance(cfg.bump_window_s, float)
    assert cfg.grace_period_s == 30.0
    assert isinstance(cfg.grace_period_s, float)


def test_integer_float_field_is_coerced_to_float(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("grace_period_s: 30\n")
    cfg = Config.load(path)
    assert isinstance(cfg.grace_period_s, float)


def test_out_of_range_port_falls_back_to_default(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("web_port: 99999\n")
    with caplog.at_level(logging.WARNING):
        assert Config.load(path).web_port == 8080
    assert "web_port" in caplog.text


def test_negative_timings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("grace_period_s: -5\nbump_window_s: -1\n")
    cfg = Config.load(path)
    assert cfg.grace_period_s == Config().grace_period_s
    assert cfg.bump_window_s == Config().bump_window_s


def test_non_string_url_falls_back_to_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("owntone_url: 3689\nreader_device: []\n")
    cfg = Config.load(path)
    assert cfg.owntone_url == "http://127.0.0.1:3689"
    assert cfg.reader_device == "tty:AMA0:pn532"


def test_non_string_path_falls_back_to_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("library_root: []\n")
    assert Config.load(path).library_root == Path("/srv/music")


def test_boolean_is_not_accepted_as_a_port(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web_port: true\n")
    assert Config.load(path).web_port == 8080


def test_non_finite_timing_falls_back_to_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("grace_period_s: .nan\nbump_window_s: .inf\n")
    cfg = Config.load(path)
    assert cfg.grace_period_s == Config().grace_period_s
    assert cfg.bump_window_s == Config().bump_window_s


# --- reset_gpio -------------------------------------------------------------


def test_reset_gpio_defaults_to_the_jumpered_pin(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web_port: 8080\n")
    assert Config.load(path).reset_gpio == 20


def test_reset_gpio_can_be_moved_to_another_pin(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio: 17\n")
    assert Config.load(path).reset_gpio == 17


def test_null_reset_gpio_disables_the_reset(tmp_path):
    """A board without the RSTPDN jumper must be able to opt out."""
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio: null\n")
    assert Config.load(path).reset_gpio is None


def test_empty_reset_gpio_disables_the_reset(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio:\n")
    assert Config.load(path).reset_gpio is None


def test_reset_gpio_none_keyword_disables_the_reset(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('reset_gpio: "none"\n')
    assert Config.load(path).reset_gpio is None


def test_out_of_range_reset_gpio_falls_back_to_default(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio: 99\n")
    with caplog.at_level(logging.WARNING):
        assert Config.load(path).reset_gpio == 20
    assert "reset_gpio" in caplog.text


def test_non_numeric_reset_gpio_falls_back_to_default(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio: [20]\n")
    with caplog.at_level(logging.WARNING):
        assert Config.load(path).reset_gpio == 20
    assert "reset_gpio" in caplog.text


def test_boolean_is_not_accepted_as_a_reset_gpio(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("reset_gpio: true\n")
    assert Config.load(path).reset_gpio == 20
