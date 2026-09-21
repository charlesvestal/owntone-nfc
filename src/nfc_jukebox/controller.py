"""The vinyl state machine.

Put the record on and it plays. Take it off and it pauses - lifting a card is
how you pause a record player, and pausing must not lose your place. The record
stays on the platter until a different one is put on: the same card always
resumes, a different card clears the queue and starts from track 1, and an
album that plays out to its end leaves no place to hold.

The grace period exists because AirPlay 2's PTP handshake costs a couple of
seconds, so the session is held through short gaps and the speakers are handed
back only when the user is genuinely done. That release now deselects the
outputs *without* stopping or clearing the queue, which is what keeps the
position alive across it.

Two time sources, deliberately. Every duration - the grace period, the polling
intervals - is measured on a monotonic clock, so an NTP step cannot strand a
paused card or fire a release an hour early. The nightly resume reset is the
one question a monotonic clock cannot answer ("has 3am happened yet?"), so it,
and only it, reads a wall clock.
"""
from __future__ import annotations

import datetime
import enum
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from .cards import normalise_uid

log = logging.getLogger(__name__)

# How often, while an album is playing, to ask OwnTone whether the outputs we
# asked for are still the outputs it is using.
#
# Not every tick: tick() runs once a second for as long as the box is powered
# on, and a per-tick check would be two HTTP round trips a second, forever, to
# watch for something that happens once at the start of a record. Not at card
# tap either: the AirPlay 2 failure this exists to catch lands *after* every
# synchronous call has returned success, and the tap path is already ~3s of
# latency that must not grow.
#
# 10s is chosen from the failure's own timing: OwnTone's pairing attempt and
# the self-deselect that follows it play out over a few seconds, so the first
# check lands after the dust settles, and the operator learns the truth about
# twelve seconds into side one rather than at the end of it.
OUTPUT_CHECK_INTERVAL_S = 10.0

# How often, while an album is playing, to ask OwnTone whether it still is.
#
# Shares the output check's cadence and its reasoning: one cheap GET on the
# same 10s beat rather than a second polling rhythm to reason about. The event
# being watched for - the last groove running out - is not urgent, it just has
# to be noticed before the next card lands, and nobody taps a card within ten
# seconds of side two ending.
PLAYBACK_CHECK_INTERVAL_S = OUTPUT_CHECK_INTERVAL_S

# What OwnTone reports in /api/player once the queue has played out. A card
# lifted mid-track gives "pause"; only a finished (or stopped) queue gives
# this. Confusing the two would throw the place away on every lift.
_FINISHED_STATE = "stop"


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
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime.datetime]
                 = datetime.datetime.now) -> None:
        self._owntone = owntone
        self._cards = cards
        self._snapshot = snapshot
        self._config = config
        self._clock = clock
        # Local wall-clock time, and used for exactly one thing: which side of
        # the nightly reset hour we are on. Kept separate from `clock` rather
        # than replacing it, because every *duration* here must stay on the
        # monotonic clock - an NTP step mid-evening would otherwise either fire
        # a release instantly or park a paused card for hours. Injected so the
        # reset can be tested without sleeping or moving the system clock.
        self._wall_clock = wall_clock

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
        # Management mode: cards identify themselves but do not play. Sitting
        # down to register a stack of cards while each tap starts an album is
        # the wrong experience, especially with the speakers in a living room.
        # Deliberately NOT persisted - a restart returns to a working jukebox,
        # because a box that silently refuses to play is worse than one that
        # forgot a setting.
        self.management_mode = False
        self.last_error: str | None = None
        self.now_playing: str | None = None
        # Every scanned UID, known or not — this is what learn mode reads.
        self.last_seen_uid: str | None = None
        self.last_seen_at: float = 0.0
        # The card on the platter right now: what the reader last announced and
        # we acted on. Cleared whenever we stop believing anything is playing.
        self._last_uid: str | None = None

        # Which card's album is sitting in OwnTone's queue, and when it was put
        # there. Deliberately separate from `state` and from `_last_uid`: the
        # queue outlives both, which is the whole point. A release hands the
        # speakers back and leaves this alone, so the next tap of the same card
        # resumes; a different card, a finished album, and management mode all
        # clear it, so the next tap of *those* starts from track 1.
        #
        # In memory only, and deliberately so. The position it refers to lives
        # in OwnTone's queue, which does not survive OwnTone restarting or the
        # Pi rebooting either - persisting the memory of a position that is
        # gone would only produce a confident resume into silence. Restarting
        # the service therefore loses your place, which is the same thing
        # lifting the tonearm and switching the amp off does.
        #
        # `_loaded_at` is a WALL-clock stamp, unlike every other timestamp in
        # this class, because the only question asked of it is whether the
        # reset hour has fallen between then and now.
        self._loaded_uid: str | None = None
        self._loaded_at: datetime.datetime | None = None
        # True until this process has acted on its first recognised card.
        # Adoption keys off this rather than off `_loaded_uid` being None,
        # which is also true after an album has played itself out -- and
        # adopting *then* would resume a finished record instead of starting
        # it over.
        self._fresh_start = True

        self._paused_at = 0.0
        # The last selection we declined to save because it had only shrunk.
        # See _save_selection.
        self._last_shrunken_selection: list[str] | None = None

        # Intended outputs that have gone missing from OwnTone's list and that
        # we are still waiting on. See _check_outputs: a speaker off the
        # network comes back, and re-selecting it then is worth doing.
        self._awaiting_return: set[str] = set()

        # The outputs *we* last wrote, so that the next card can tell "the user
        # changed this in OwnTone's web UI" from "this is the state we left
        # behind ourselves". Written only where we call set_outputs, plus by
        # _check_outputs when it observes the selection changing under us
        # mid-album. See _restore_outputs.
        #
        # Deliberately in memory only, unlike the speaker snapshot. The
        # snapshot is a record of the user's *choice*, which must outlive a
        # reboot. This is a record of what the running process did, and a
        # process that was not there cannot vouch for anything: across a
        # restart the box was off, OwnTone restarted too and restored its own
        # last-written selection (only on a clean shutdown -- a stale row there
        # has already caused real confusion), and any of it could have been
        # changed by hand in between. None therefore means "no idea", and
        # _restore_outputs answers that by applying the snapshot: with no
        # evidence either way, the choice the user actually saved beats a
        # selection nobody can vouch for. Hand-picked selections are honoured
        # again from the next card on, once there is something to compare to.
        self._last_set_outputs: set[str] | None = None

        # The outputs we believe should be selected right now, or None when
        # there is nothing to watch (idle, or already reported). See
        # _check_outputs.
        self._intended_outputs: set[str] | None = None
        self._next_output_check = 0.0
        self._next_playback_check = 0.0
        # Bumped whenever the intended set changes, so a check that started
        # before a new card arrived cannot publish its stale answer.
        self._outputs_generation = 0

    # --- events -----------------------------------------------------------

    def on_card_present(self, uid: str) -> None:
        with self._lock:
            self._on_card_present(uid)

    def _on_card_present(self, uid: str) -> None:
        # Recorded before the lookup so unregistered cards are still learnable.
        self.last_seen_uid = uid
        self.last_seen_at = self._clock()

        # Management mode short-circuits here: after recording the UID, so the
        # card is still learnable, but before anything reaches OwnTone.
        if self.management_mode:
            self._note_identified(uid)
            return

        # A still-seated card re-announced by the reader. Restarting the album
        # mid-listen because the reader spoke twice would be maddening, so do
        # nothing at all.
        if self.state is State.PLAYING and uid == self._last_uid:
            return

        card = self._cards.get(uid)
        if card is None:
            self.last_error = f"Unknown card {uid}"
            log.warning(self.last_error)
            return

        # First card since this service started, and OwnTone may never have
        # stopped: it outlives us, so a redeploy can leave the record still
        # turning. Take that over rather than clearing the queue and dropping
        # the needle on side one again. Only ever for the album actually
        # queued, and only when we have no memory of our own to trust.
        fresh, self._fresh_start = self._fresh_start, False
        if fresh and self._adopt_playing_album(uid, card):
            return

        # A recognised card is a fresh start, so the status line starts clean:
        # anything that goes wrong below overwrites this. Otherwise one
        # "Unknown card" from a hotel key card sits on the admin page forever,
        # through every subsequent success.
        self.last_error = None

        if self._is_resumable(uid):
            self._resume(card, uid)
        else:
            self._start(card, uid)

    def _start(self, card, uid: str) -> None:
        """Put this record on from track 1. Caller holds the lock."""
        intended = self._select_outputs(card)

        with self._guarded("forcing shuffle and repeat off"):
            self._owntone.set_vinyl_playback_mode()

        with self._guarded(f"starting {card.name}") as outcome:
            self._owntone.play_album(card.path)
        if not outcome.ok:
            # Nothing is playing, so do not claim otherwise - and nothing is
            # loaded either, so the next tap must not try to resume into
            # whatever the failed call left behind.
            self._watch_outputs(None)
            self.state = State.IDLE
            self.now_playing = None
            self._last_uid = None
            self._forget_loaded()
            return

        self._watch_outputs(intended)
        self._last_uid = uid
        self._loaded_uid = uid
        self._loaded_at = self._wall_clock()
        self._next_playback_check = self._clock() + PLAYBACK_CHECK_INTERVAL_S
        self.now_playing = card.name
        self.state = State.PLAYING

    def _resume(self, card, uid: str) -> None:
        """Pick the needle back up where it was. Caller holds the lock.

        The outputs are re-selected first because a release has very likely
        happened since: the whole point of it is to hand the HomePods back, and
        resuming into the local soundcard because nobody took them again would
        be a quiet, baffling failure.
        """
        intended = self._select_outputs(card)

        with self._guarded("forcing shuffle and repeat off"):
            self._owntone.set_vinyl_playback_mode()

        with self._guarded(f"resuming {card.name}") as outcome:
            self._owntone.play()
        if not outcome.ok:
            # Stay as we are: the queue still holds the place, so a re-present
            # can simply try again. Nothing is forgotten on a failed resume.
            return

        self._watch_outputs(intended)
        self._last_uid = uid
        self._next_playback_check = self._clock() + PLAYBACK_CHECK_INTERVAL_S
        self.now_playing = card.name
        self.state = State.PLAYING

    def _select_outputs(self, card) -> list[str] | None:
        """Choose where this record should be audible. Caller holds the lock.

        Three separate failure domains, on purpose. Choosing the outputs is
        best effort; playing the record is the contract. A HomePod that is
        still in OwnTone's list from a cached mDNS record but is powered off
        makes `/api/outputs/set` fail, and that must not be allowed to skip
        playback and leave a card sitting on the platter in silence.

        Returns what we end up believing should be audible. None means we never
        managed to work it out, and there is then nothing to verify against.
        """
        intended: list[str] | None = None
        with self._guarded(f"selecting outputs for {card.name}") as outcome:
            intended = self._restore_outputs()
        if outcome.ok:
            return intended

        intended = None
        with self._guarded("falling back to local outputs"):
            local = self._owntone.local_output_ids()
            self._set_outputs(local)
            intended = local
        return intended

    def start_over(self) -> str:
        """Restart the loaded album from track 1, and return its name.

        The admin page's one transport control, and it exists because this
        design took the other one away: lifting a card used to be how you got
        back to the start, and now it is how you pause. Nothing else can
        rewind a record mid-side.

        Raises LookupError when there is nothing on the platter, and
        RuntimeError when OwnTone refused. Deliberately louder than the reader
        paths, which contain everything: this one is called by a person who is
        standing there waiting to be told whether it worked.
        """
        with self._lock:
            uid = self._loaded_uid
            card = self._cards.get(uid) if uid else None
            if card is None:
                raise LookupError(
                    "Nothing is loaded to start over. Tap a card first.")
            self._start(card, uid)
            if self.state is not State.PLAYING:
                raise RuntimeError(self.last_error
                                   or f"Could not start {card.name} over")
            return card.name

    def _adopt_playing_album(self, uid: str, card) -> bool:
        """Take over an album OwnTone is already playing. True if adopted.

        Deliberately narrow: it answers "is this exact record on the
        turntable", and anything unexpected -- OwnTone unreachable, a
        different album, stopped -- falls through to the normal path, which
        loads the album properly.
        """
        try:
            state = (self._owntone.player_state() or {}).get("state")
            if state not in ("play", "pause"):
                return False
            if not self._owntone.queue_holds_album(card.path):
                return False
        except Exception:                                   # noqa: BLE001
            # Never let this optimisation be the thing that stops a card
            # working: fall back to loading the album.
            log.debug("Could not check what is playing", exc_info=True)
            return False

        if state == "pause":
            with self._guarded("resuming the album already loaded"):
                self._owntone.play()
        self._loaded_uid = uid
        self._loaded_at = self._wall_clock()
        self._last_uid = uid
        self.now_playing = card.name
        self.state = State.PLAYING
        self.last_error = None
        log.info("Adopted the album already playing: %s", card.name)
        return True

    def note_scan(self, uid: str) -> None:
        """Record a card seen by some reader other than the jukebox's own.

        Identification only: it never starts, stops or switches a record, so a
        scan at a desk cannot interrupt what is playing in the room. That is
        the same contract management mode gives a tap on the box itself, which
        is why both go through here.
        """
        with self._lock:
            self._note_identified(normalise_uid(uid))

    def _note_identified(self, uid: str) -> None:
        """Remember a card for registration, and say whether we know it."""
        self.last_seen_uid = uid
        self.last_seen_at = self._clock()
        card = self._cards.get(uid)
        self.last_error = None if card else f"Unknown card {uid}"

    def set_management_mode(self, enabled: bool) -> None:
        """Turn card-identification-only mode on or off.

        What changes is what a card *read* means: in management mode a card
        identifies itself and nothing else. It does not start an album, does
        not pause on removal, and does not switch records.

        Playback is deliberately untouched, entering or leaving. This used to
        stop the record and clear the queue, reasoning that you want a quiet
        box to register against -- but registering happens while music is
        playing, and silencing the room because someone opened the admin page
        is worse than the noise. The mode governs the reader, not the player.
        """
        with self._lock:
            self.management_mode = bool(enabled)

    def on_card_removed(self) -> None:
        with self._lock:
            self._on_card_removed()

    def _on_card_removed(self) -> None:
        if self.management_mode:
            return
        if self.state is not State.PLAYING:
            return
        # PLAYING does not mean something is playing. `_check_finished` leaves
        # the state alone when the side runs out - that is what keeps a
        # re-announced card a no-op - and forgets the loaded record instead.
        # Pausing an OwnTone whose queue has emptied earns a 500, which turns
        # the ordinary end of a record into a red error on the admin page.
        if self._loaded_uid is not None:
            with self._guarded("pausing"):
                self._owntone.pause()
        # PAUSED regardless: the card is off the platter, and whether the pause
        # failed or there was simply nothing to pause, the grace timer still
        # has to stop and release the outputs.
        self._paused_at = self._clock()
        self.state = State.PAUSED

    def tick(self) -> None:
        """Called periodically; releases the outputs once grace expires,
        verifies that what we are playing to is what we asked for, and notices
        when the side has run out."""
        self._tick_grace()
        self._check_outputs()
        self._check_finished()

    def _tick_grace(self) -> None:
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
            #
            # `_loaded_uid` survives this on purpose. IDLE means "no speakers
            # held, nothing audible", not "nothing on the platter" - the album
            # and its position are still in OwnTone's queue, waiting for the
            # same card to come back.
            self._watch_outputs(None)
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

    # --- output selection --------------------------------------------------

    def _set_outputs(self, ids: list[str]) -> None:
        """Write the selection, and remember that the write was ours.

        Every place that selects outputs goes through here. A write that is not
        remembered looks like a user edit at the next card, which would mean
        never restoring the snapshot again; and a remembered write that never
        happened would mean overriding a choice we mistook for our own. The
        record is only updated once OwnTone has accepted the call: if it
        raises, what is selected is anybody's guess, and "no idea" is the
        safer thing to be left believing.

        Caller holds the lock.
        """
        self._owntone.set_outputs(ids)
        self._last_set_outputs = set(ids)

    # --- output verification ----------------------------------------------

    def _watch_outputs(self, intended: list[str] | None) -> None:
        """Start (or stop, with None) watching a set of outputs.

        Caller holds the lock.
        """
        self._intended_outputs = set(intended) if intended else None
        self._awaiting_return = set()
        self._next_output_check = self._clock() + OUTPUT_CHECK_INTERVAL_S
        self._outputs_generation += 1

    def _check_outputs(self) -> None:
        """Notice, after the fact, that we are not playing where we asked to.

        Every call on the card path can return success and the record can
        still come out of the wrong speaker: an AirPlay 2 receiver refuses
        pairing seconds later, deselects itself, and OwnTone quietly falls back
        to the local soundcard. `requires_auth`/`needs_auth_key` were both
        False on the real devices while OwnTone's log demanded a PIN, so the
        only observable is that what we asked for is no longer selected.
        """
        with self._lock:
            if self.state is not State.PLAYING:
                return
            intended = self._intended_outputs
            if not intended:
                return
            now = self._clock()
            if now < self._next_output_check:
                return
            self._next_output_check = now + OUTPUT_CHECK_INTERVAL_S
            generation = self._outputs_generation

        # Outside the lock, deliberately. Everything else in this file that
        # talks to OwnTone does so holding it, because those calls are also
        # *mutating* state and must be atomic against the reader thread. This
        # one only reads, so parking the reader -- and with it the card tap --
        # behind two HTTP round trips would buy nothing. _guarded is not used
        # for the same reason: it writes last_error, and writes happen below,
        # under the lock, once we know the answer is still current.
        try:
            known = set(self._owntone.all_output_ids())
            selected = set(self._owntone.selected_output_ids())
            gained = selected - intended
            # Only asked for when something was gained, to keep the steady
            # state at two round trips per interval.
            local = set(self._owntone.local_output_ids()) if gained else set()
        except Exception as exc:  # see _guarded for why this is so broad
            message = f"OwnTone error while checking the selected outputs: {exc}"
            with self._lock:
                self.last_error = message
            log.warning(message, exc_info=True)
            return

        with self._lock:
            # A new card (or a release) landed while we were asking. That
            # answer describes the previous record; publishing it would put a
            # stale complaint on a fresh, healthy album.
            if self._outputs_generation != generation:
                return

            # Is this a person, or a failure? Nothing in the API says so
            # directly -- a deliberate deselect and a self-deselecting HomePod
            # look identical. But a *gain* is evidence: OwnTone's fallback only
            # ever lands on the local soundcard, so a newly selected output
            # that is not local is something a human chose in the web UI
            # mid-album. Honour it: adopt the new selection as what we now
            # intend to be hearing, and say nothing.
            if gained - local:
                self._intended_outputs = set(selected)
                self._last_set_outputs = set(selected)
                self._outputs_generation += 1
                return

            # A speaker we were waiting on is back in OwnTone's list. Give it
            # one more chance to pair, rather than making the listener lift the
            # card to get stereo back. Bounded by construction: this fires only
            # on the absent -> present transition, and if the receiver then
            # refuses, it is `deselected` on the next pass and the watch drops.
            returning = self._awaiting_return & known
            if returning:
                self._awaiting_return -= returning
                names = ", ".join(sorted(returning))
                try:
                    self._set_outputs(sorted(intended))
                except Exception as exc:  # see _guarded for why this is broad
                    self.last_error = (
                        f"OwnTone error while re-selecting {names}: {exc}")
                    log.warning(self.last_error, exc_info=True)
                else:
                    self.last_error = None
                    log.info("Output(s) %s came back; re-selected them", names)
                return

            lost = intended - selected
            if not lost:
                self._awaiting_return.clear()
                return

            # Gone from the list is not the same as refusing to pair. A speaker
            # off the network said nothing at all, and those come back, so keep
            # watching for it instead of dropping the watch. Reported once on
            # the way out and then quietly, or one HomePod's absence becomes a
            # warning every interval for the rest of the side.
            missing = lost - known
            if missing and not (lost & known):
                newly = missing - self._awaiting_return
                self._awaiting_return |= missing
                self._last_set_outputs = set(selected)
                if newly:
                    self.last_error = (
                        f"Output(s) {', '.join(sorted(newly))} are no longer "
                        "known to OwnTone; the record is playing without them"
                    )
                    log.warning(self.last_error)
                return

            # Whatever this was, we have now *seen* it happen under us, so it
            # is no longer something the next card should read as a hand edit
            # made at the web UI. This is what keeps a speaker that drops
            # mid-album out of the deliberate-change branch in
            # _restore_outputs: the next card compares against the shrunken
            # reality, finds no difference, and restores the snapshot -- which
            # re-selects the speaker and gives it another chance to pair.
            self._last_set_outputs = set(selected)

            vanished = sorted(lost - known)
            deselected = sorted(lost & known)
            if vanished:
                self.last_error = (
                    f"Output(s) {', '.join(vanished)} are no longer known to "
                    "OwnTone; the record is playing without them"
                )
            else:
                self.last_error = (
                    f"Output(s) {', '.join(deselected)} deselected themselves "
                    "after playback started (an AirPlay receiver refusing to "
                    "pair does this); the record is playing to "
                    f"{', '.join(sorted(selected)) or 'nothing'} instead"
                )
            log.warning(self.last_error)

            # Reported once and then dropped, for two reasons. Re-selecting a
            # receiver that just refused pairing will refuse again, so retrying
            # would be a loop that never converges and only delays the truth.
            # And re-reporting every interval would bury the rest of the log in
            # one HomePod's sulk. The next card re-arms this.
            self._intended_outputs = None
            self._outputs_generation += 1

    # --- is this record still on the platter? ------------------------------

    def _forget_loaded(self) -> None:
        """Nothing is in the queue any more. Caller holds the lock."""
        self._loaded_uid = None
        self._loaded_at = None

    def _is_resumable(self, uid: str) -> bool:
        """Should this card pick up where it left off, or start side one?

        Three things have to hold, and each is its own kind of evidence:
        it is the same record; a new day has not begun under it; and OwnTone
        still actually has the queue. The last is asked of OwnTone rather than
        remembered, because the position lives there and only there.

        Caller holds the lock.
        """
        if uid != self._loaded_uid:
            return False
        if self._reset_hour_has_passed():
            self._forget_loaded()
            return False
        if not self._queue_is_loaded():
            self._forget_loaded()
            return False
        return True

    def _reset_hour_has_passed(self) -> bool:
        """Has the nightly reset fallen between loading the album and now?

        The user's case: a record started at eleven and not finished. At ten
        the next morning it should be side one again, not the back half of side
        two. An hour of the day rather than a duration, because what is being
        modelled is "a new day", and 3am is when nobody is listening.

        Naive local time throughout. The reset is a matter of household
        routine, not of instants, so the DST-ambiguous hour is worth exactly
        nothing to defend against: the failure it could produce, once or twice
        a year, is a resume that was offered an hour late or withdrawn an hour
        early.

        Caller holds the lock.
        """
        hour = self._config.resume_reset_hour
        if hour is None or self._loaded_at is None:
            return False
        now = self._wall_clock()
        boundary = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if boundary > now:
            # Today's reset has not happened yet, so the one that matters is
            # yesterday's. This is what keeps 23:00 -> 02:00 the same evening.
            boundary -= datetime.timedelta(days=1)
        return self._loaded_at < boundary

    def _queue_is_loaded(self) -> bool:
        """Does OwnTone still hold the album we think it does?

        OwnTone can be restarted under a running jukebox, which empties the
        queue while our memory of it survives; resuming then would be a
        confident press of play into silence.

        A failed check counts as "not loaded". The consequence is a restart
        from track 1, which is audible and self-explanatory; the consequence of
        guessing the other way is a silent box. Logged rather than written to
        `last_error` for the same reason: nothing the operator needs to act on
        has happened, and the status line is the one slot there is for things
        that have.

        Caller holds the lock.
        """
        try:
            return self._owntone.queue_length() > 0
        except Exception:  # see _guarded for why this is so broad
            log.warning("Could not read the queue from OwnTone; starting the "
                        "album from the beginning rather than risk silence",
                        exc_info=True)
            return False

    def _check_finished(self) -> None:
        """Notice that the side has run out, so the next tap starts side one.

        Folded onto the output check's beat rather than given a rhythm of its
        own: both are "ask OwnTone what is really going on" and neither is
        urgent. It has to be noticed before the next card, not before the next
        second.

        Playback is deliberately not touched. The card may well still be on the
        reader - the record has simply finished - and `state` is left alone so
        that a re-announced card is still the no-op it always was, rather than
        restarting the album the moment it ends, forever.
        """
        with self._lock:
            if self.state is not State.PLAYING or self._loaded_uid is None:
                return
            now = self._clock()
            if now < self._next_playback_check:
                return
            self._next_playback_check = now + PLAYBACK_CHECK_INTERVAL_S
            loaded = self._loaded_uid

        # Outside the lock, for the reasons _check_outputs sets out at length.
        try:
            state = str(self._owntone.player_state().get("state", ""))
        except Exception as exc:  # see _guarded for why this is so broad
            message = f"OwnTone error while checking playback: {exc}"
            with self._lock:
                self.last_error = message
            log.warning(message, exc_info=True)
            return

        if state.casefold() != _FINISHED_STATE:
            return

        with self._lock:
            # A card (or a release) landed while we were asking, so this answer
            # is about a record that is no longer the one loaded.
            if self._loaded_uid != loaded or self.state is not State.PLAYING:
                return
            log.info("Album finished; the next tap will start it from track 1")
            self._forget_loaded()

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
        if shrunk:
            missing = set(saved) - set(selected)
            # A deselect and a drop are only indistinguishable while the output
            # is still listed. Deselecting in the web UI leaves the speaker
            # there, unselected; only a fault takes it out of OwnTone's list
            # altogether -- the distinction _check_outputs already draws
            # between `deselected` and `vanished`. So a shrink whose missing
            # members are gone entirely is never somebody's choice, and must
            # not stick however often it repeats.
            #
            # Found 2026-09-21: a night of Wi-Fi trouble repeatedly took a
            # HomePod out of OwnTone's list, the repeat-shrink rule below read
            # that as intent, and rewrote a stereo pair down to one speaker.
            # The next card then restored one speaker and the record played in
            # mono, with nothing on the page to say why.
            try:
                known = set(self._owntone.all_output_ids())
            except Exception:  # see _guarded for why this is so broad
                # Cannot tell. Keeping a speaker we should have forgotten is a
                # far cheaper mistake than forgetting one we should have kept.
                known = set()
            if not missing <= known:
                gone = ", ".join(sorted(missing - known))
                self.last_error = (
                    f"Output(s) {gone} are not in OwnTone's list at all, so "
                    "this is a drop and not a choice; keeping the saved "
                    "speakers."
                )
                log.warning(self.last_error)
                return
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
        """Hand the speakers back, and leave the record where it is.

        This used to stop playback and clear the queue. It must not: the queue
        *is* the position, and destroying it here is what made a lifted card
        lose its place. The player is already paused (on_card_removed), so
        deselecting the AirPlay outputs is enough to end the session and let
        the HomePods go idle for other senders - which was always the part of
        this that mattered.
        """
        self._save_selection(self._owntone.selected_output_ids())
        # Keep local selected, drop AirPlay so the HomePods go idle and are
        # free for other senders. Through _set_outputs, because this is the
        # selection the next card will find and must recognise as our own.
        self._set_outputs(self._owntone.local_output_ids())

    def _restore_outputs(self) -> list[str]:
        """Select the outputs this record should play to, and return them.

        The return value is what we *intend* to be audible, which is not always
        what we wrote: on the paths that deliberately leave the selection
        alone, it is whatever the user already had selected. That is still the
        thing to watch -- a hand-picked HomePod that refuses to pair is exactly
        the failure this feeds.
        """
        current = self._owntone.selected_output_ids()

        # Guard rail: never override a selection a person made. The question is
        # only how to tell one, and the answer is to compare what OwnTone
        # reports against what we last wrote ourselves (_set_outputs). If they
        # differ, somebody has been at the web UI since -- so adopt their
        # choice as the snapshot and leave the selection alone.
        #
        # Symmetric on purpose. The older rule asked instead whether anything
        # non-local was selected, reasoning that only an AirPlay speaker can be
        # deliberate and that local-only is merely our own post-release state.
        # That is true right up until the user deliberately picks the local
        # output because they want headphones: the snapshot then overrode them
        # and the sound jumped back to the HomePods -- an override that has
        # already woken somebody in the house. A Chromecast or the HTTP stream
        # is covered by the same comparison, without needing to be named.
        #
        # An empty selection is not evidence of anything (the same judgement
        # _save_selection makes), so it falls through to the snapshot: there is
        # nothing there to clobber, and selecting nothing guarantees silence.
        # Nothing written by this process yet -- the first card after a start --
        # falls straight through to the snapshot below. That selection is not
        # evidence of a human choice: it is either the local-only state a
        # previous run left behind at release, or a row OwnTone persisted on
        # its last clean shutdown. Deferring to it put the first record of the
        # evening through the Pi's headphone jack instead of the HomePods,
        # which is the exact failure the snapshot exists to prevent. The
        # snapshot is still not *adopted* from that selection -- it is applied,
        # and survives unchanged.
        if current and self._last_set_outputs is not None:
            if set(current) != self._last_set_outputs:
                # Through _save_selection rather than straight to the snapshot,
                # so that the shrunken-selection policy still applies: a
                # speaker that merely dropped must not quietly delete itself
                # from the saved choice. The playback selection is honoured
                # either way -- refusing to overwrite the snapshot is not a
                # reason to move somebody's audio.
                self._save_selection(current)
                return current

        desired = self._snapshot.load()
        if not desired:
            return current

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
                return current

        self._set_outputs(usable)
        return usable
