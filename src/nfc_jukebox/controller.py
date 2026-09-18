"""The vinyl state machine.

Put the record on, it plays from the start. Take it off, it stops. The grace
period exists because AirPlay 2's PTP handshake costs a couple of seconds, so
the session is held through short gaps and released only when the user is
genuinely done.
"""
from __future__ import annotations

import enum
import logging
import time
from contextlib import contextmanager
from typing import Callable, Iterator

import httpx

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class _Outcome:
    """Whether the guarded OwnTone interaction actually got through."""

    __slots__ = ("ok",)

    def __init__(self) -> None:
        self.ok = True


class Controller:
    def __init__(self, owntone, cards, snapshot, config,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._owntone = owntone
        self._cards = cards
        self._snapshot = snapshot
        self._config = config
        self._clock = clock

        self.state = State.IDLE
        self.last_error: str | None = None
        self.now_playing: str | None = None
        # Every scanned UID, known or not — this is what learn mode reads.
        self.last_seen_uid: str | None = None
        self.last_seen_at: float = 0.0
        self._last_uid: str | None = None
        self._paused_at = 0.0

    # --- events -----------------------------------------------------------

    def on_card_present(self, uid: str) -> None:
        # Recorded before the lookup so unregistered cards are still learnable.
        self.last_seen_uid = uid
        self.last_seen_at = self._clock()

        # A still-seated card re-announced by the reader. Restarting the album
        # from track 1 mid-listen is exactly what the bump window exists to
        # prevent, so do nothing at all.
        if self.state is State.PLAYING and uid == self._last_uid:
            return

        card = self._cards.get(uid)
        if card is None:
            self.last_error = f"Unknown card {uid}"
            log.warning(self.last_error)
            return

        if self._is_bump(uid):
            with self._guarded("resuming playback") as outcome:
                self._owntone.play()
            if outcome.ok:
                self.state = State.PLAYING
            # Otherwise stay PAUSED: the grace timer still owns the session,
            # and a re-present can try again.
            return

        with self._guarded(f"starting {card.name}") as outcome:
            self._restore_outputs()
            self._owntone.play_album(card.path)
        if not outcome.ok:
            # Nothing is playing, so do not claim otherwise.
            self.state = State.IDLE
            self.now_playing = None
            self._last_uid = None
            return

        self._last_uid = uid
        self.now_playing = card.name
        self.state = State.PLAYING

    def on_card_removed(self) -> None:
        if self.state is not State.PLAYING:
            return
        with self._guarded("pausing"):
            self._owntone.pause()
        # PAUSED regardless: the card is off the platter, and if the pause did
        # not land the grace timer will still stop and release the outputs.
        self._paused_at = self._clock()
        self.state = State.PAUSED

    def tick(self) -> None:
        """Called periodically; releases the outputs once grace expires."""
        if self.state is not State.PAUSED:
            return
        if self._clock() - self._paused_at > self._config.grace_period_s:
            with self._guarded("releasing outputs"):
                self._release()
            # Back to IDLE even on failure: the user is done with this record,
            # and retrying the release on every tick would only spam the log.
            self.state = State.IDLE
            self.now_playing = None
            self._last_uid = None

    # --- internals --------------------------------------------------------

    @contextmanager
    def _guarded(self, what: str) -> Iterator[_Outcome]:
        """Contain an OwnTone transport/HTTP failure.

        An exception escaping into the reader callback tears the reader down
        and reopens it for no reason, and leaves the admin page with nothing
        to show. Record it instead.
        """
        outcome = _Outcome()
        try:
            yield outcome
        except httpx.HTTPError as exc:
            outcome.ok = False
            self.last_error = f"OwnTone error while {what}: {exc}"
            log.warning(self.last_error, exc_info=True)

    def _is_bump(self, uid: str) -> bool:
        """A dropped read, not a deliberate lift — resume rather than restart."""
        return (
            self.state is State.PAUSED
            and uid == self._last_uid
            and self._clock() - self._paused_at <= self._config.bump_window_s
        )

    def _release(self) -> None:
        selected = self._owntone.selected_output_ids()
        if selected:
            self._snapshot.save(selected)
        self._owntone.stop()
        self._owntone.clear_queue()
        # Keep local selected, drop AirPlay so the HomePods go idle and are
        # free for other senders.
        self._owntone.set_outputs(self._owntone.local_output_ids())

    def _restore_outputs(self) -> None:
        airplay = set(self._owntone.airplay_output_ids())
        current = self._owntone.selected_output_ids()

        # Guard rail: an AirPlay output selected by hand while idle is a
        # deliberate choice. Never clobber it with the snapshot. Local-only
        # selection is just our own post-release state, so it does not count.
        if any(output_id in airplay for output_id in current):
            return

        desired = self._snapshot.load()
        if not desired:
            return

        available = airplay | set(self._owntone.local_output_ids())
        usable = [output_id for output_id in desired if output_id in available]
        if not usable:
            self.last_error = "Saved outputs unavailable; falling back to local"
            log.warning(self.last_error)
            usable = self._owntone.local_output_ids()

        self._owntone.set_outputs(usable)
