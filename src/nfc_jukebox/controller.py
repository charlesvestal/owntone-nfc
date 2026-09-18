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
        self._last_uid: str | None = None
        self._paused_at = 0.0
        # The last selection we declined to save because it had only shrunk.
        # See _save_selection.
        self._last_shrunken_selection: list[str] | None = None

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
        # _restore_outputs treats that as the user's, which costs one album
        # played to whatever is already selected and never moves the sound
        # somewhere nobody asked for. The persisted snapshot still takes effect
        # from the next card on.
        self._last_set_outputs: set[str] | None = None

        # The outputs we believe should be selected right now, or None when
        # there is nothing to watch (idle, or already reported). See
        # _check_outputs.
        self._intended_outputs: set[str] | None = None
        self._next_output_check = 0.0
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
            card = self._cards.get(uid)
            self.last_error = None if card else f"Unknown card {uid}"
            return

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
        #
        # What we end up believing should be audible. None means we never
        # managed to work it out, and there is then nothing to verify against.
        intended: list[str] | None = None
        with self._guarded(f"selecting outputs for {card.name}") as outputs_outcome:
            intended = self._restore_outputs()
        if not outputs_outcome.ok:
            intended = None
            with self._guarded("falling back to local outputs"):
                local = self._owntone.local_output_ids()
                self._set_outputs(local)
                intended = local

        with self._guarded("forcing shuffle and repeat off"):
            self._owntone.set_vinyl_playback_mode()

        with self._guarded(f"starting {card.name}") as outcome:
            self._owntone.play_album(card.path)
        if not outcome.ok:
            # Nothing is playing, so do not claim otherwise.
            self._watch_outputs(None)
            self.state = State.IDLE
            self.now_playing = None
            self._last_uid = None
            return

        self._watch_outputs(intended)
        self._last_uid = uid
        self.now_playing = card.name
        self.state = State.PLAYING

    def set_management_mode(self, enabled: bool) -> None:
        """Turn card-identification-only mode on or off.

        Enabling stops playback: the point is a quiet box to register against,
        and leaving the current album running would defeat it.
        """
        with self._lock:
            was = self.management_mode
            self.management_mode = bool(enabled)
            if self.management_mode and not was:
                with self._guarded("stopping for management mode"):
                    self._owntone.stop()
                    self._owntone.clear_queue()
                self.state = State.IDLE
                self.now_playing = None
                self._last_uid = None

    def on_card_removed(self) -> None:
        with self._lock:
            self._on_card_removed()

    def _on_card_removed(self) -> None:
        if self.management_mode:
            return
        if self.state is not State.PLAYING:
            return
        with self._guarded("pausing"):
            self._owntone.pause()
        # PAUSED regardless: the card is off the platter, and if the pause did
        # not land the grace timer will still stop and release the outputs.
        self._paused_at = self._clock()
        self.state = State.PAUSED

    def tick(self) -> None:
        """Called periodically; releases the outputs once grace expires, and
        verifies that what we are playing to is what we asked for."""
        self._tick_grace()
        self._check_outputs()

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

            lost = intended - selected
            if not lost:
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
        if current:
            if self._last_set_outputs is None:
                # Nothing written by this process yet -- first card after a
                # start. We cannot claim this selection as ours, so we treat it
                # as the user's and leave it be. Note what we do *not* do:
                # adopt it into the snapshot. OwnTone's own idea of the
                # selection is not authoritative (it persists it only on a
                # clean shutdown), and letting a stale row overwrite the
                # snapshot would destroy the choice the snapshot exists to
                # carry across exactly this reboot. It is restored from the
                # next card on, once a release has given us a record.
                return current
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
