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


# --- Presence debouncing ----------------------------------------------------
#
# Measured on hardware (2026-09-18) with two motionless cards over 25s:
#
#   NTAG213      044ef792816b81  one continuous hold, 11s+, 35ms worst jitter
#   Mifare-ish   cc7da7ee        725 present/absent cycles, median hold 0.008s
#
# The 4-byte card's "absence" is an artefact of re-selection failing, not of
# the card moving. Undebounced, it produced ~29 present/removed pairs a second
# and an unbroken play/pause storm at OwnTone, so the user heard silence while
# the status said "playing". The reader must therefore emit level-triggered
# events that both technologies agree on.


class StopPolling(Exception):
    """Raised by the fake frontend to end an otherwise infinite poll loop."""


class FakeTarget:
    """What nfcpy's clf.sense() hands back for a type-A target."""

    def __init__(self, uid: str) -> None:
        self.sdd_res = bytes.fromhex(uid)


class FakeClf:
    """Fake nfcpy frontend replaying a scripted per-poll presence sequence."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.senses = 0

    def sense(self, *targets, **kwargs):
        if self.senses >= len(self.script):
            raise StopPolling
        uid = self.script[self.senses]
        self.senses += 1
        return FakeTarget(uid) if uid is not None else None

    def close(self):
        pass


class Clock:
    """A fake monotonic clock that only advances when the reader sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run_poll(script, debounce=None, monkeypatch=None):
    """Drive _poll_forever over a scripted sequence; return timestamped events."""
    clock = Clock()
    monkeypatch.setattr(reader_mod.time, "sleep", clock.sleep)
    kwargs = {} if debounce is None else {"presence_debounce_s": debounce}
    reader = Pn532Reader("tty:AMA0:pn532", clock=clock.monotonic, **kwargs)
    events = []
    reader.on_present = lambda uid: events.append(("present", uid, clock.now))
    reader.on_removed = lambda: events.append(("removed", None, clock.now))
    clf = FakeClf(script)
    with pytest.raises(StopPolling):
        reader._poll_forever(clf, targets=["106A"])
    return events, clock


NTAG = "044ef792816b81"
MIFARE = "cc7da7ee"


def test_flickering_card_reports_one_unbroken_presence(monkeypatch):
    """The 8ms-cycle card: absent on every other poll, never actually moved."""
    script = [MIFARE, None] * 300
    events, _ = run_poll(script, monkeypatch=monkeypatch)
    assert [(kind, uid) for kind, uid, _ in events] == [("present", MIFARE)]


def test_continuous_card_reports_one_clean_pair(monkeypatch):
    """The NTAG213 case: solidly present, then genuinely lifted."""
    script = [NTAG] * 300 + [None] * 100
    events, _ = run_poll(script, monkeypatch=monkeypatch)
    assert [(kind, uid) for kind, uid, _ in events] == [
        ("present", NTAG), ("removed", None),
    ]


def test_removal_is_reported_only_after_the_debounce(monkeypatch):
    debounce = 0.5
    script = [NTAG] * 10 + [None] * 200
    events, _ = run_poll(script, debounce=debounce, monkeypatch=monkeypatch)

    kinds = [e[0] for e in events]
    assert kinds == ["present", "removed"]
    last_seen = (10 - 1) * reader_mod.POLL_INTERVAL
    removed_at = events[1][2]
    assert removed_at >= last_seen + debounce
    assert removed_at < last_seen + debounce + 2 * reader_mod.POLL_INTERVAL


def test_debounce_interval_is_configurable(monkeypatch):
    script = [NTAG] * 10 + [None] * 200
    quick, _ = run_poll(script, debounce=0.1, monkeypatch=monkeypatch)
    slow, _ = run_poll(script, debounce=1.0, monkeypatch=monkeypatch)
    assert quick[1][2] < slow[1][2]


def test_a_different_card_switches_immediately(monkeypatch):
    """A deliberate swap must not wait out the debounce."""
    script = [NTAG] * 5 + [MIFARE] * 5
    events, _ = run_poll(script, debounce=5.0, monkeypatch=monkeypatch)
    assert [(kind, uid) for kind, uid, _ in events] == [
        ("present", NTAG), ("present", MIFARE),
    ]
    # Announced on the very poll it first appeared, not a debounce later.
    assert events[1][2] == 5 * reader_mod.POLL_INTERVAL


def test_a_replaced_card_after_a_real_removal_is_a_fresh_present(monkeypatch):
    script = [NTAG] * 5 + [None] * 200 + [NTAG] * 5
    events, _ = run_poll(script, debounce=0.5, monkeypatch=monkeypatch)
    assert [(kind, uid) for kind, uid, _ in events] == [
        ("present", NTAG), ("removed", None), ("present", NTAG),
    ]


def test_uid_is_normalised(monkeypatch):
    events, _ = run_poll([MIFARE.upper()], monkeypatch=monkeypatch)
    assert events[0][1] == MIFARE


def test_default_debounce_rides_out_the_measured_flicker():
    """Default must cover far more than the 35ms worst-case observed gap."""
    assert Pn532Reader("tty:AMA0:pn532")._debounce >= 0.3
    # ...and still stop the music well inside a second of a lift.
    assert Pn532Reader("tty:AMA0:pn532")._debounce <= 0.75
