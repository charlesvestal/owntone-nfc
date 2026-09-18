# tests/test_config.py
from pathlib import Path

from nfc_jukebox.config import Config


def test_defaults_when_file_missing(tmp_path):
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.owntone_url == "http://127.0.0.1:3689"
    assert cfg.library_root == Path("/srv/music")
    assert cfg.grace_period_s == 90.0


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
