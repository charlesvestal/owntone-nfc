# tests/test_reader.py
import logging
import subprocess

import pytest

from nfc_jukebox import reader as reader_mod
from nfc_jukebox.reader import FakeReader, Pn532Reader


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


# --- Hardware reset recovery ------------------------------------------------
#
# A SIGTERM mid-transaction leaves the PN532 out of frame sync; every
# subsequent ContactlessFrontend() then times out forever. Pulsing RSTPDN
# (GPIO20 on this HAT, active low) is the only thing that clears it.


class FakeNfc:
    """Stand-in for the lazily imported nfc module."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def ContactlessFrontend(self, device):  # noqa: N802 - mirrors nfcpy
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("[Errno 110] Connection timed out")
        return f"clf:{device}"


@pytest.fixture
def slept(monkeypatch):
    naps = []
    monkeypatch.setattr(reader_mod.time, "sleep", naps.append)
    return naps


@pytest.fixture
def pinctrl(monkeypatch):
    """Record pinctrl invocations; never run the real thing."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(reader_mod.subprocess, "run", fake_run)
    return calls


def test_clean_open_never_touches_the_reset_line(slept, pinctrl):
    reader = Pn532Reader("tty:AMA0:pn532")
    assert reader._open_frontend(FakeNfc(failures=0)) == "clf:tty:AMA0:pn532"
    assert pinctrl == []


def test_a_single_failure_retries_without_resetting(slept, pinctrl):
    """One timeout may be a transient that self-resolves; don't reset yet."""
    reader = Pn532Reader("tty:AMA0:pn532")
    assert reader._open_frontend(FakeNfc(failures=1)) == "clf:tty:AMA0:pn532"
    assert pinctrl == []


def test_second_consecutive_failure_pulses_the_reset_line(slept, pinctrl):
    reader = Pn532Reader("tty:AMA0:pn532", reset_gpio=20)
    assert reader._open_frontend(FakeNfc(failures=2)) == "clf:tty:AMA0:pn532"
    assert pinctrl == [
        ["pinctrl", "set", "20", "op", "dl"],
        ["pinctrl", "set", "20", "op", "dh"],
    ]


def test_reset_uses_the_configured_pin(slept, pinctrl):
    reader = Pn532Reader("tty:AMA0:pn532", reset_gpio=7)
    reader._open_frontend(FakeNfc(failures=2))
    assert [c[2] for c in pinctrl] == ["7", "7"]


def test_reset_holds_the_line_low_before_releasing_it(monkeypatch, pinctrl):
    order = []
    monkeypatch.setattr(reader_mod.time, "sleep", lambda s: order.append(("sleep", s)))

    real_run = reader_mod.subprocess.run

    def watching_run(cmd, **kwargs):
        order.append(("pinctrl", cmd[-1]))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(reader_mod.subprocess, "run", watching_run)
    Pn532Reader("tty:AMA0:pn532")._pulse_reset()
    assert order == [
        ("pinctrl", "dl"),
        ("sleep", reader_mod.RESET_PULSE_S),
        ("pinctrl", "dh"),
    ]


def test_disabled_reset_never_shells_out(slept, pinctrl):
    reader = Pn532Reader("tty:AMA0:pn532", reset_gpio=None)
    assert reader._open_frontend(FakeNfc(failures=5)) == "clf:tty:AMA0:pn532"
    assert pinctrl == []


def test_pinctrl_nonzero_exit_does_not_propagate(monkeypatch, slept, caplog):
    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "pinctrl: permission denied")

    monkeypatch.setattr(reader_mod.subprocess, "run", failing_run)
    reader = Pn532Reader("tty:AMA0:pn532")
    with caplog.at_level(logging.ERROR):
        assert reader._open_frontend(FakeNfc(failures=2)) == "clf:tty:AMA0:pn532"
    assert "permission denied" in caplog.text


def test_missing_pinctrl_does_not_propagate(monkeypatch, slept, caplog):
    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(reader_mod.subprocess, "run", missing)
    reader = Pn532Reader("tty:AMA0:pn532")
    with caplog.at_level(logging.ERROR):
        assert reader._open_frontend(FakeNfc(failures=2)) == "clf:tty:AMA0:pn532"
    assert "pinctrl" in caplog.text


def test_pinctrl_timeout_does_not_propagate(monkeypatch, slept):
    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(reader_mod.subprocess, "run", timing_out)
    reader = Pn532Reader("tty:AMA0:pn532")
    assert reader._open_frontend(FakeNfc(failures=2)) == "clf:tty:AMA0:pn532"


def test_retry_backoff_grows_and_is_capped(slept, pinctrl):
    reader = Pn532Reader("tty:AMA0:pn532", reset_gpio=None)
    reader._open_frontend(FakeNfc(failures=8))
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert max(slept) == reader_mod.MAX_BACKOFF


def test_journal_distinguishes_retrying_from_reset_attempted(slept, pinctrl, caplog):
    reader = Pn532Reader("tty:AMA0:pn532", reset_gpio=20)
    with caplog.at_level(logging.INFO):
        reader._open_frontend(FakeNfc(failures=2))
    text = caplog.text
    assert "Cannot open reader" in text          # the retry
    assert "reset" in text.lower()               # the recovery attempt
    assert "GPIO20" in text                      # which pin
    assert "after 2 failed" in text              # the recovery itself


def test_default_reset_pin_is_the_jumpered_one():
    assert Pn532Reader("tty:AMA0:pn532")._reset_gpio == 20
