# tests/test_outputs.py
from nfc_jukebox.outputs import OutputSnapshot


def test_round_trip(tmp_path):
    snap = OutputSnapshot(tmp_path / "outputs.json")
    snap.save(["1", "3"])
    assert snap.load() == ["1", "3"]


def test_missing_file_returns_empty(tmp_path):
    assert OutputSnapshot(tmp_path / "nope.json").load() == []


def test_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "outputs.json"
    path.write_text("{not json")
    assert OutputSnapshot(path).load() == []


def test_save_creates_parent_directory(tmp_path):
    snap = OutputSnapshot(tmp_path / "nested" / "dir" / "outputs.json")
    snap.save(["7"])
    assert snap.load() == ["7"]
