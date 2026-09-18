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
from typing import Callable

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


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

        card = self._cards.get(uid)
        if card is None:
            self.last_error = f"Unknown card {uid}"
            log.warning(self.last_error)
            return

        if self._is_bump(uid):
            self._owntone.play()
            self.state = State.PLAYING
            return

        self._restore_outputs()
        self._owntone.play_album(card.path)
        self._last_uid = uid
        self.now_playing = card.name
        self.state = State.PLAYING

    def on_card_removed(self) -> None:
        if self.state is not State.PLAYING:
            return
        self._owntone.pause()
        self._paused_at = self._clock()
        self.state = State.PAUSED

    def tick(self) -> None:
        """Called periodically; releases the outputs once grace expires."""
        if self.state is not State.PAUSED:
            return
        if self._clock() - self._paused_at > self._config.grace_period_s:
            self._release()

    # --- internals --------------------------------------------------------

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
        self.state = State.IDLE
        self.now_playing = None
        self._last_uid = None

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
