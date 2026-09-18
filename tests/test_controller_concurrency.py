"""Concurrency tests for Controller.

Three threads share one Controller in __main__.py: the reader thread (card
present/removed), the tick thread (grace expiry), and Flask's worker threads
(reading status). The dangerous moment is grace expiry, which is exactly when
somebody is standing at the box changing the record.
"""
from __future__ import annotations

import threading

import pytest

from nfc_jukebox.cards import Card
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State

from .test_controller import FakeCards, FakeClock, FakeOwnTone, FakeSnapshot

# Generous enough for a loaded CI box, short enough that a regression fails
# fast instead of hanging the suite.
JOIN_TIMEOUT = 5.0
# Only ever waited out on the *fixed* code path, where the reader thread is
# correctly parked on the lock and therefore never signals.
RACE_WINDOW = 0.25


class BlockingOwnTone(FakeOwnTone):
    """set_outputs() parks inside _release() until the test lets it go.

    The release no longer stops playback - that is what preserves the place on
    the record - so the slow call it now parks on is the one that hands the
    speakers back.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release_entered = threading.Event()
        self.may_release = threading.Event()
        self.play_album_called = threading.Event()

    def set_outputs(self, ids):
        self.release_entered.set()
        assert self.may_release.wait(JOIN_TIMEOUT), \
            "test never released set_outputs()"
        super().set_outputs(ids)

    def play_album(self, path):
        super().play_album(path)
        self.play_album_called.set()


@pytest.fixture
def ctx():
    clock = FakeClock()
    owntone = BlockingOwnTone()
    cards = FakeCards({
        "aaaa": Card(uid="aaaa", name="Blue", path="Miles Davis/Kind of Blue"),
        "bbbb": Card(uid="bbbb", name="Rumours", path="Fleetwood Mac/Rumours"),
    })
    config = Config(grace_period_s=90.0)
    controller = Controller(owntone, cards, FakeSnapshot(), config, clock=clock)
    return controller, owntone, clock


def _run(fn, *args):
    thread = threading.Thread(target=fn, args=args, daemon=True)
    thread.start()
    return thread


def test_card_placed_during_release_is_not_silently_stopped(ctx):
    """The defect: tick() decides to release, a card arrives mid-release, and
    tick() then stamps IDLE over the freshly started album. The box goes silent
    while believing nothing is playing."""
    controller, owntone, clock = ctx

    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)

    ticker = _run(controller.tick)
    assert owntone.release_entered.wait(JOIN_TIMEOUT), "tick never reached _release"

    reader = _run(controller.on_card_present, "bbbb")
    # Unsynchronised, the reader races straight through here and its state is
    # then clobbered. Synchronised, it is parked and this simply times out.
    owntone.play_album_called.wait(RACE_WINDOW)

    owntone.may_release.set()
    ticker.join(JOIN_TIMEOUT)
    reader.join(JOIN_TIMEOUT)
    assert not ticker.is_alive() and not reader.is_alive()

    assert ("play_album", "Fleetwood Mac/Rumours") in owntone.calls
    assert controller.state is State.PLAYING
    assert controller.now_playing == "Rumours"
    assert controller.last_seen_uid == "bbbb"


def test_status_reads_are_not_blocked_by_a_slow_release(ctx):
    """Flask's threads must be able to render the admin page while _release()
    is waiting on OwnTone, so the status endpoint deliberately reads without
    the lock."""
    controller, owntone, clock = ctx

    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)

    ticker = _run(controller.tick)
    assert owntone.release_entered.wait(JOIN_TIMEOUT), "tick never reached _release"

    read = {}

    def read_status():
        read["snapshot"] = (
            controller.state,
            controller.now_playing,
            controller.last_error,
            controller.last_seen_uid,
        )

    status = _run(read_status)
    status.join(RACE_WINDOW)
    assert not status.is_alive(), "/api/status would hang during a slow release"
    assert read["snapshot"][0] in tuple(State)

    owntone.may_release.set()
    ticker.join(JOIN_TIMEOUT)
    assert not ticker.is_alive()
