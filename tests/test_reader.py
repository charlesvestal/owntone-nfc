# tests/test_reader.py
from nfc_jukebox.reader import FakeReader


def test_fake_reader_emits_present_and_removed():
    events = []
    reader = FakeReader()
    reader.on_present = lambda uid: events.append(("present", uid))
    reader.on_removed = lambda: events.append(("removed",))

    reader.place("04:A2:B3:C4")
    reader.lift()

    assert events == [("present", "04a2b3c4"), ("removed",)]


def test_fake_reader_normalises_uid():
    seen = []
    reader = FakeReader()
    reader.on_present = seen.append
    reader.place("04A2B3C4")
    assert seen == ["04a2b3c4"]


def test_lift_without_place_is_ignored():
    events = []
    reader = FakeReader()
    reader.on_removed = lambda: events.append("removed")
    reader.lift()
    assert events == []
