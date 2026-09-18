"""The vinyl state machine.

Put the record on, it plays from the start. Take it off, it stops. The grace
period exists because AirPlay 2's PTP handshake costs a couple of seconds, so
the session is held through short gaps and released only when the user is
genuinely done.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

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

        # Three threads share one Controller: the reader (present/removed),
        # the tick thread (grace expiry) and Flask's workers (status reads).
        # Re-entrant because the public methods call internal helpers and one
        # of those may one day call another public method.
        #
        # The lock guards *mutation* only. The four public attributes are read
        # without it from /api/status: each is a single attribute load, which
        # cannot tear, and the alternative is an admin page that hangs for as
        # long as _release() spends inside OwnTone. A status page that catches
        # the state a moment before now_playing is a cosmetic flicker; a status
        # page that will not load is the operator's only window going dark.
        self._lock = threading.RLock()

        self.state = State.IDLE
        self.last_error: str | None = None
        self.now_playing: str | None = None
        # Every scanned UID, known or not — this is what learn mode reads.
        self.last_seen_uid: str | None = None
        self.last_seen_at: float = 0.0
        self._last_uid: str | None = None
        self._paused_at = 0.0
        # The last selection we declined to save because it had only shrunk.
        # See _save_selection.
        self._last_shrunken_selection: list[str] | None = None

    # --- events -----------------------------------------------------------

    def on_card_present(self, uid: str) -> None:
        with self._lock:
            self._on_card_present(uid)

    def _on_card_present(self, uid: str) -> None:
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

        # A recognised card is a fresh start, so the status line starts clean:
        # anything that goes wrong below overwrites this. Otherwise one
        # "Unknown card" from a hotel key card sits on the admin page forever,
        # through every subsequent success.
        self.last_error = None

        if self._is_bump(uid):
            with self._guarded("resuming playback") as outcome:
                self._owntone.play()
            if outcome.ok:
                self.state = State.PLAYING
            # Otherwise stay PAUSED: the grace timer still owns the session,
            # and a re-present can try again.
            return

        # Three separate failure domains, on purpose. Choosing the outputs is
        # best effort; playing the record is the contract. A HomePod that is
        # still in OwnTone's list from a cached mDNS record but is powered off
        # makes `/api/outputs/set` fail, and that must not be allowed to skip
        # playback and leave a card sitting on the platter in silence.
        with self._guarded(f"selecting outputs for {card.name}") as outputs_outcome:
            self._restore_outputs()
        if not outputs_outcome.ok:
            with self._guarded("falling back to local outputs"):
                self._owntone.set_outputs(self._owntone.local_output_ids())

        with self._guarded("forcing shuffle and repeat off"):
            self._owntone.set_vinyl_playback_mode()

        with self._guarded(f"starting {card.name}") as outcome:
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
        with self._lock:
            self._on_card_removed()

    def _on_card_removed(self) -> None:
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
        # The decision to release and the IDLE stamp that follows it must be
        # one atomic step. Otherwise a card placed while _release() is still
        # talking to OwnTone starts an album that this method then silently
        # stops and forgets, leaving the box quiet and claiming to be IDLE --
        # and grace expiry is precisely when somebody is changing the record.
        #
        # Held across the OwnTone calls rather than dropped and re-checked:
        # the client carries a 3s timeout, so this cannot block forever, and
        # a reader event that waits its turn is far better than one that
        # interleaves halfway through a release.
        with self._lock:
            if self.state is not State.PAUSED:
                return
            if self._clock() - self._paused_at <= self._config.grace_period_s:
                return
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
        """Contain any failure of an interaction with OwnTone.

        An exception escaping into the reader callback tears the reader down
        and reopens it for no reason, and leaves the admin page with nothing
        to show. Record it instead.

        Deliberately `Exception`, not `httpx.HTTPError`. HTTPError covers the
        transport and the status code, but not the body: `_outputs()` does
        `.json()["outputs"]`, so a 200 carrying HTML -- a reverse proxy, or
        OwnTone answering an unknown path with its SPA index -- raises
        JSONDecodeError (a ValueError) or KeyError. Enumerating the exception
        types OwnTone might provoke is a guess we would keep getting wrong, and
        getting it wrong kills the service thread. Everything inside a guarded
        block is one OwnTone interaction whose correct response to any failure
        is identical: record it, surface it, carry on degraded.

        BaseException still escapes, so KeyboardInterrupt and SystemExit shut
        the service down as they should.
        """
        outcome = _Outcome()
        try:
            yield outcome
        except Exception as exc:
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

    def _save_selection(self, selected: list[str]) -> None:
        """Persist the user's speaker choice, defending it against a drop.

        An empty read is never intent. Neither, usually, is a *shrunken* one:
        a HomePod that vanishes mid-album -- a Wi-Fi blip, or the documented
        `ANNOUNCE request failed ... 400 Bad Request` self-deselect -- leaves
        OwnTone reporting local-only, and saving that would forget the speaker
        permanently and silently.

        A deliberate deselect and a dropped speaker are indistinguishable in
        the API: both leave the output present and `selected: false`. So this
        is a policy, not a detection. A shrink is ignored the first time and
        the operator is told why; if the same shrunken selection comes back
        after a full restore cycle -- we re-selected the speaker at card start
        and it is gone again at release -- that is the user really having
        chosen, and it sticks.
        """
        if not selected:
            return
        saved = self._snapshot.load()
        shrunk = set(selected) < set(saved)
        if shrunk and self._last_shrunken_selection != sorted(selected):
            self._last_shrunken_selection = sorted(selected)
            missing = ", ".join(sorted(set(saved) - set(selected)))
            self.last_error = (
                f"Output(s) {missing} were not selected at release; keeping the "
                "saved speaker choice in case they only dropped. Change the "
                "selection again to make it stick."
            )
            log.warning(self.last_error)
            return
        self._last_shrunken_selection = None
        self._snapshot.save(selected)

    def _release(self) -> None:
        self._save_selection(self._owntone.selected_output_ids())
        self._owntone.stop()
        self._owntone.clear_queue()
        # Keep local selected, drop AirPlay so the HomePods go idle and are
        # free for other senders.
        self._owntone.set_outputs(self._owntone.local_output_ids())

    def _restore_outputs(self) -> None:
        local = set(self._owntone.local_output_ids())
        current = self._owntone.selected_output_ids()

        # Guard rail: anything non-local selected by hand while idle is a
        # deliberate choice -- an AirPlay speaker, but equally a Chromecast or
        # the HTTP stream, which are neither local nor AirPlay and which we
        # would otherwise silently drop. Never clobber it with the snapshot.
        # A local-only selection is just our own post-release state, so it
        # does not count.
        if any(output_id not in local for output_id in current):
            return

        desired = self._snapshot.load()
        if not desired:
            return

        available = set(self._owntone.all_output_ids())
        usable = [output_id for output_id in desired if output_id in available]
        if not usable:
            self.last_error = "Saved outputs unavailable; falling back to local"
            log.warning(self.last_error)
            usable = self._owntone.local_output_ids()
            if not usable:
                # No soundcard either. Selecting nothing would guarantee
                # silence; leave whatever OwnTone has and let playback try.
                self.last_error = "No usable outputs; playing to whatever is selected"
                log.warning(self.last_error)
                return

        self._owntone.set_outputs(usable)
