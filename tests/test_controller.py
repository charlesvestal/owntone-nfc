import httpx
import pytest

from nfc_jukebox.cards import Card
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State
from nfc_jukebox.owntone import is_airplay, is_local


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOwnTone:
    """A stand-in for the REST client.

    The `type` strings are OwnTone's real ones, and the classification
    predicates are imported from the production module rather than re-guessed
    here: fakes that reimplement the thing under test are how the AirPlay
    release bug survived a green suite.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.outputs = [
            {"id": "1", "type": "ALSA", "selected": True},
            {"id": "2", "type": "AirPlay 2", "selected": False},
        ]

    def _ids(self, predicate):
        return [o["id"] for o in self.outputs if predicate(o)]

    def selected_output_ids(self):
        return self._ids(lambda o: o["selected"])

    def airplay_output_ids(self):
        return self._ids(is_airplay)

    def local_output_ids(self):
        return self._ids(is_local)

    def all_output_ids(self):
        return self._ids(lambda o: True)

    def set_vinyl_playback_mode(self):
        self.calls.append(("set_vinyl_playback_mode",))

    def set_outputs(self, ids):
        self.calls.append(("set_outputs", tuple(ids)))
        for o in self.outputs:
            o["selected"] = o["id"] in ids

    def play_album(self, path):
        self.calls.append(("play_album", path))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def clear_queue(self):
        self.calls.append(("clear_queue",))


class FakeCards:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, uid):
        return self._mapping.get(uid)


class FakeSnapshot:
    def __init__(self, initial=None):
        self.value = initial or []

    def load(self):
        return list(self.value)

    def save(self, ids):
        self.value = list(ids)


@pytest.fixture
def ctx():
    clock = FakeClock()
    owntone = FakeOwnTone()
    cards = FakeCards({
        "aaaa": Card(uid="aaaa", name="Blue", path="Miles Davis/Kind of Blue"),
        "bbbb": Card(uid="bbbb", name="Rumours", path="Fleetwood Mac/Rumours"),
    })
    snapshot = FakeSnapshot()
    config = Config(bump_window_s=0.5, grace_period_s=90.0)
    controller = Controller(owntone, cards, snapshot, config, clock=clock)
    return controller, owntone, snapshot, clock


def test_card_placed_starts_album(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING


def test_card_removed_pauses_immediately(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    assert owntone.calls[-1] == ("pause",)
    assert controller.state is State.PAUSED


def test_same_card_within_bump_window_resumes(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(0.2)
    controller.on_card_present("aaaa")
    assert owntone.calls[-1] == ("play",)
    album_calls = [c for c in owntone.calls if c[0] == "play_album"]
    assert album_calls == [("play_album", "Miles Davis/Kind of Blue")]


def test_same_card_after_bump_window_restarts(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(5.0)
    controller.on_card_present("aaaa")
    assert sum(1 for c in owntone.calls if c[0] == "play_album") == 2


def test_different_card_switches_immediately(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    controller.on_card_present("bbbb")
    assert ("play_album", "Fleetwood Mac/Rumours") in owntone.calls


def test_grace_expiry_stops_and_releases_airplay(ctx):
    controller, owntone, _, clock = ctx
    owntone.outputs[1]["selected"] = True  # AirPlay selected
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert ("stop",) in owntone.calls
    assert ("clear_queue",) in owntone.calls
    assert owntone.selected_output_ids() == ["1"]  # AirPlay deselected
    assert controller.state is State.IDLE


def test_snapshot_restored_on_next_card(ctx):
    controller, owntone, snapshot, clock = ctx
    owntone.outputs[1]["selected"] = True
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert snapshot.load() == ["1", "2"]
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1", "2"]


def test_manual_airplay_selection_while_idle_is_not_clobbered(ctx):
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)  # we last selected local
    snapshot.save(["1"])
    owntone.outputs[1]["selected"] = True  # user picked AirPlay by hand
    controller.on_card_present("aaaa")
    # Assert the *absence of the write*, not the resulting state: the state was
    # set up two lines above, so asserting it would pass even if
    # _restore_outputs were replaced with `return`.
    assert not any(c[0] == "set_outputs" for c in owntone.calls)
    assert owntone.selected_output_ids() == ["1", "2"]


def test_manual_chromecast_selection_while_idle_is_not_clobbered(ctx):
    """A choice that is neither local nor AirPlay needs no special case: it
    differs from what we last selected, which is the whole test."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    owntone.outputs.append({"id": "7", "type": "Chromecast", "selected": True})
    owntone.outputs[0]["selected"] = False
    snapshot.save(["1"])
    controller.on_card_present("aaaa")
    assert not any(c[0] == "set_outputs" for c in owntone.calls)


def test_missing_saved_output_falls_back_to_local(ctx):
    controller, owntone, snapshot, clock = ctx
    # Past the first cycle, so the local-only selection is known to be ours
    # and the snapshot is what we act on. See _prime.
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["99"])  # HomePod no longer on the network
    controller.on_card_present("aaaa")
    # The fallback must be an actual write of the local outputs, not merely the
    # local-only selection the fixture already starts in.
    assert ("set_outputs", ("1",)) in owntone.calls
    assert owntone.selected_output_ids() == ["1"]
    assert controller.last_error is not None
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING


def test_release_selects_only_real_local_outputs(ctx):
    """A release must not hand the record to a Chromecast or to the HTTP
    stream just because they are not AirPlay."""
    controller, owntone, _, clock = ctx
    owntone.outputs += [
        {"id": "7", "type": "Chromecast", "selected": False},
        {"id": "8", "type": "streaming", "selected": False},
    ]
    owntone.outputs[1]["selected"] = True
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert owntone.selected_output_ids() == ["1"]


def test_starting_an_album_forces_shuffle_and_repeat_off(ctx):
    """OwnTone persists shuffle across restarts and its UI has a one-click
    toggle. With shuffle on, `playback=start` picks a random track and the
    vinyl contract breaks invisibly."""
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    assert ("set_vinyl_playback_mode",) in owntone.calls
    order = [c[0] for c in owntone.calls]
    assert order.index("set_vinyl_playback_mode") < order.index("play_album")


def test_vinyl_mode_failure_still_plays(ctx):
    controller, owntone, _, _ = ctx
    owntone.set_vinyl_playback_mode = _boom
    controller.on_card_present("aaaa")
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING
    assert controller.last_error is not None


def test_last_error_is_cleared_by_a_successful_start(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("ffff")  # hotel key card
    assert controller.last_error is not None
    controller.on_card_present("aaaa")
    assert controller.state is State.PLAYING
    # Otherwise one stray tap sits on the admin status line forever.
    assert controller.last_error is None


def test_last_error_is_cleared_by_a_successful_bump_resume(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    controller.last_error = "stale"
    clock.advance(0.2)
    controller.on_card_present("aaaa")
    assert controller.state is State.PLAYING
    assert controller.last_error is None


def test_a_shrunken_selection_does_not_destroy_the_saved_choice(ctx):
    """The HomePod drops mid-album (Wi-Fi blip, or the documented
    `ANNOUNCE ... 400 Bad Request` self-deselect). OwnTone now reports local
    only. Saving that over the snapshot would forget the speaker for good."""
    controller, owntone, snapshot, clock = ctx
    snapshot.save(["1", "2"])
    owntone.outputs[1]["selected"] = True
    controller.on_card_present("aaaa")
    owntone.outputs[1]["selected"] = False  # the speaker drops off
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert snapshot.load() == ["1", "2"]
    # Never silently: the operator gets told why the snapshot was kept.
    assert controller.last_error is not None


def test_a_repeated_shrink_is_taken_as_a_deliberate_change(ctx):
    """One shrink is a drop; the same shrink surviving a full restore cycle is
    the user really having chosen local-only, and must eventually stick."""
    controller, owntone, snapshot, clock = ctx
    snapshot.save(["1", "2"])
    owntone.outputs[1]["selected"] = True

    for _ in range(2):
        controller.on_card_present("aaaa")
        owntone.outputs[1]["selected"] = False  # user deselects it again
        controller.on_card_removed()
        clock.advance(91.0)
        controller.tick()

    assert snapshot.load() == ["1"]


def test_a_grown_selection_is_saved_immediately(ctx):
    controller, owntone, snapshot, clock = ctx
    snapshot.save(["1"])
    controller.on_card_present("aaaa")
    owntone.outputs[1]["selected"] = True  # user adds the HomePod mid-album
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert snapshot.load() == ["1", "2"]


def test_unknown_card_is_ignored(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("ffff")
    assert not any(c[0] == "play_album" for c in owntone.calls)
    assert controller.last_error is not None
    assert controller.state is State.IDLE


def test_unknown_card_is_still_recorded_for_learn_mode(ctx):
    controller, _, _, _ = ctx
    controller.on_card_present("ffff")
    assert controller.last_seen_uid == "ffff"


def test_same_card_re_presented_while_playing_is_a_noop(ctx):
    """A double-fired present event for a still-seated card must not restart."""
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    before = list(owntone.calls)
    clock.advance(30.0)  # well past the bump window; the card never left
    controller.on_card_present("aaaa")
    assert owntone.calls == before
    assert controller.state is State.PLAYING
    assert controller.last_seen_uid == "aaaa"
    assert controller.last_seen_at == clock.now


def _boom(*_args, **_kwargs):
    """Stand-in for what owntone.py raises on any 4xx/5xx."""
    request = httpx.Request("PUT", "http://test:3689/api/player/play")
    raise httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )


def test_play_album_failure_does_not_escape_and_is_recorded(ctx):
    controller, owntone, _, _ = ctx
    owntone.play_album = _boom
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    # Never claim PLAYING when playback demonstrably failed to start.
    assert controller.state is not State.PLAYING
    assert controller.now_playing is None


def test_bump_resume_failure_does_not_escape(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(0.2)
    owntone.play = _boom
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    assert controller.state is not State.PLAYING


def test_restore_outputs_failure_still_plays_locally(ctx):
    """A dead HomePod (still in OwnTone's list via cached mDNS, 500s on
    `/api/outputs/set`) must not make the box silent. The spec: play locally
    anyway and surface it. Output selection is best-effort; playback is the
    contract."""
    controller, owntone, _, _ = ctx
    owntone.selected_output_ids = _boom
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING
    assert controller.now_playing == "Blue"
    # ...and having failed to work out the right outputs, fall back to local.
    assert ("set_outputs", ("1",)) in owntone.calls


def test_total_output_failure_still_plays(ctx):
    """Even the local fallback failing must not stop the record."""
    controller, owntone, _, _ = ctx
    owntone.selected_output_ids = _boom
    owntone.set_outputs = _boom
    controller.on_card_present("aaaa")
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING


def test_non_http_failure_is_contained(ctx):
    """A 200 with a non-JSON body (a reverse proxy, or OwnTone serving its SPA
    index) raises JSONDecodeError/KeyError, not httpx.HTTPError. It must not
    escape into the reader thread."""
    controller, owntone, _, _ = ctx

    def bad_shape(*_a, **_k):
        raise KeyError("outputs")

    owntone.selected_output_ids = bad_shape
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    assert controller.state is State.PLAYING


def test_playback_failure_of_an_unexpected_kind_is_contained(ctx):
    controller, owntone, _, _ = ctx

    def bad_shape(*_a, **_k):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    owntone.play_album = bad_shape
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    assert controller.state is not State.PLAYING


def test_keyboard_interrupt_is_not_swallowed(ctx):
    """Containment is for OwnTone misbehaving, not for shutdown signals."""
    controller, owntone, _, _ = ctx

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    owntone.play_album = interrupted
    with pytest.raises(KeyboardInterrupt):
        controller.on_card_present("aaaa")


def test_pause_failure_does_not_escape(ctx):
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    owntone.pause = _boom
    controller.on_card_removed()
    assert controller.last_error is not None
    # Still parked in PAUSED so the grace timer can release later.
    assert controller.state is State.PAUSED


def test_release_failure_does_not_escape(ctx):
    controller, owntone, _, clock = ctx
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    owntone.stop = _boom
    clock.advance(91.0)
    controller.tick()
    assert controller.last_error is not None
    assert controller.state is State.IDLE


# --- output verification ---------------------------------------------------
#
# Every synchronous call at card-tap time can succeed and the box can still end
# up playing to the wrong speaker: an AirPlay 2 HomePod refuses pairing several
# seconds later, deselects itself, and OwnTone quietly falls back to the local
# soundcard. `state=play`, `last_error=None`, and the user is listening to the
# wrong thing. These tests pin the after-the-fact check that notices.


def _start_with_both_outputs(ctx):
    """Start an album with local + AirPlay selected, as the snapshot asks."""
    controller, owntone, snapshot, clock = ctx
    # The snapshot is only acted on once we have a record of what we last
    # selected ourselves; the first card after a start deliberately leaves the
    # selection alone. See _prime.
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1", "2"])
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1", "2"]
    assert controller.last_error is None
    return controller, owntone, snapshot, clock


def test_intended_output_deselected_after_start_is_reported(ctx):
    """The observed defect: the HomePod deselects itself post-pairing and
    OwnTone falls back to local. Nothing synchronous can see it."""
    controller, owntone, _, clock = _start_with_both_outputs(ctx)

    owntone.outputs[1]["selected"] = False  # pairing refused, seconds later

    clock.advance(30.0)
    controller.tick()

    assert controller.state is State.PLAYING  # audio *is* playing, just wrong
    assert controller.last_error is not None
    assert "2" in controller.last_error


def test_intended_output_vanishing_after_start_is_reported(ctx):
    """An output gone from /api/outputs entirely is a different fault from one
    still listed and deselected, and must say so."""
    controller, owntone, _, clock = _start_with_both_outputs(ctx)

    del owntone.outputs[1]  # HomePod dropped off the network altogether

    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert "2" in controller.last_error
    assert "no longer" in controller.last_error


def test_a_user_switching_speakers_mid_album_is_not_an_error(ctx):
    """Changing outputs in OwnTone's own web UI is a deliberate act. Picking a
    speaker we did not ask for is the fingerprint of a human doing it."""
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    owntone.outputs.append({"id": "7", "type": "Chromecast", "selected": False})

    owntone.set_outputs(["7"])  # user moves the record to the Chromecast

    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is None
    assert controller.state is State.PLAYING


def test_a_user_switch_replaces_what_we_expect_to_stay_selected(ctx):
    """Having adopted the user's choice, we watch *that* instead."""
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    owntone.outputs.append({"id": "7", "type": "Chromecast", "selected": False})

    owntone.set_outputs(["7"])
    clock.advance(30.0)
    controller.tick()
    assert controller.last_error is None

    owntone.outputs[2]["selected"] = False  # now the Chromecast drops
    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert "7" in controller.last_error


def test_a_mismatch_is_reported_once_not_every_tick(ctx):
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    owntone.outputs[1]["selected"] = False

    clock.advance(30.0)
    controller.tick()
    assert controller.last_error is not None

    controller.last_error = None
    for _ in range(5):
        clock.advance(30.0)
        controller.tick()

    assert controller.last_error is None


def test_outputs_are_not_polled_on_every_tick(ctx):
    """One HTTP round trip per second, forever, for a box that sits idle-ish
    all evening. The check is periodic, not per-tick."""
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    owntone.outputs[1]["selected"] = False

    polls = []
    real = owntone.selected_output_ids
    owntone.selected_output_ids = lambda: (polls.append(1), real())[1]

    for _ in range(10):
        clock.advance(1.0)
        controller.tick()

    assert len(polls) <= 1


def test_a_new_card_resets_the_intended_outputs(ctx):
    controller, owntone, snapshot, clock = _start_with_both_outputs(ctx)
    owntone.outputs[1]["selected"] = False
    clock.advance(30.0)
    controller.tick()
    assert controller.last_error is not None

    controller.on_card_present("bbbb")  # fresh record, outputs re-selected
    assert controller.last_error is None
    assert owntone.selected_output_ids() == ["1", "2"]

    owntone.outputs[1]["selected"] = False  # and it drops out again
    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert "2" in controller.last_error


def test_no_output_check_once_the_record_is_released(ctx):
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()  # grace expiry: stop, clear, back to local
    assert controller.state is State.IDLE
    controller.last_error = None

    polls = []
    real = owntone.selected_output_ids
    owntone.selected_output_ids = lambda: (polls.append(1), real())[1]
    clock.advance(30.0)
    controller.tick()

    assert polls == []
    assert controller.last_error is None


def test_a_failed_output_check_does_not_escape(ctx):
    controller, owntone, _, clock = _start_with_both_outputs(ctx)
    owntone.selected_output_ids = _boom

    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert controller.state is State.PLAYING


def test_a_hand_picked_output_is_watched_too(ctx):
    """Adopting the user's selection means we never wrote one; the thing they
    chose by hand is still what we intend to be hearing."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1"])
    owntone.outputs[1]["selected"] = True  # user picked the HomePod while idle
    controller.on_card_present("aaaa")
    assert not any(c[0] == "set_outputs" for c in owntone.calls)

    owntone.outputs[1]["selected"] = False  # ...and it refuses to pair
    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert "2" in controller.last_error


def test_owntones_own_fallback_to_local_is_not_mistaken_for_a_user_choice(ctx):
    """The exact shape seen on hardware: HomePods only, pairing refused, and
    OwnTone silently selects the ALSA output instead. A *gained* output is
    normally the fingerprint of a person at the web UI -- but not when the
    thing gained is the local soundcard, which is the one place OwnTone's own
    fallback ever lands."""
    controller, owntone, snapshot, clock = ctx
    snapshot.save(["2"])  # HomePods only; nothing local wanted
    owntone.outputs[0]["selected"] = False
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["2"]

    owntone.outputs[1]["selected"] = False  # HomePod refuses to pair...
    owntone.outputs[0]["selected"] = True   # ...and OwnTone falls back

    clock.advance(30.0)
    controller.tick()

    assert controller.last_error is not None
    assert "2" in controller.last_error


def test_management_mode_identifies_cards_without_playing(ctx):
    # Registering a stack of cards while each tap starts an album is the wrong
    # experience, especially with speakers in a living room.
    controller, owntone, _, _ = ctx
    controller.set_management_mode(True)
    owntone.calls.clear()

    controller.on_card_present("aaaa")

    assert controller.last_seen_uid == "aaaa"
    assert not any(c[0] == "play_album" for c in owntone.calls)
    assert not any(c[0] == "play" for c in owntone.calls)
    assert controller.state is State.IDLE


def test_management_mode_still_learns_unregistered_cards(ctx):
    controller, _, _, _ = ctx
    controller.set_management_mode(True)
    controller.on_card_present("ffff")
    assert controller.last_seen_uid == "ffff"
    assert controller.last_error is not None


def test_management_mode_clears_the_error_for_a_known_card(ctx):
    controller, _, _, _ = ctx
    controller.set_management_mode(True)
    controller.on_card_present("ffff")
    assert controller.last_error is not None
    controller.on_card_present("aaaa")
    assert controller.last_error is None


def test_enabling_management_mode_stops_playback(ctx):
    # The point is a quiet box to register against; leaving the current album
    # running would defeat it.
    controller, owntone, _, _ = ctx
    controller.on_card_present("aaaa")
    assert controller.state is State.PLAYING

    controller.set_management_mode(True)

    assert ("stop",) in owntone.calls
    assert ("clear_queue",) in owntone.calls
    assert controller.state is State.IDLE
    assert controller.now_playing is None


def test_management_mode_ignores_card_removal(ctx):
    controller, owntone, _, _ = ctx
    controller.set_management_mode(True)
    owntone.calls.clear()
    controller.on_card_removed()
    assert not any(c[0] == "pause" for c in owntone.calls)


def test_leaving_management_mode_restores_normal_playback(ctx):
    controller, owntone, _, _ = ctx
    controller.set_management_mode(True)
    controller.set_management_mode(False)
    owntone.calls.clear()
    controller.on_card_present("aaaa")
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls
    assert controller.state is State.PLAYING


# --- who changed the selection? --------------------------------------------
#
# The user picks speakers in OwnTone's own web UI; the jukebox does not
# duplicate that control. All it has to do is tell "the user changed this" from
# "this is the state we ourselves left behind at the last release", so that a
# deliberate choice is never overridden -- in either direction.


def _prime(controller, owntone, snapshot, clock):
    """Run one full card cycle so the controller has a record of what it set.

    Fresh out of the box it has none, and then deliberately leaves the
    selection alone (see test_first_card_after_a_restart_leaves_the_selection
    _alone). Tests about the steady state need to be past that first cycle.
    The empty snapshot keeps the cycle itself from tripping the shrink policy.
    """
    snapshot.value = []
    controller.on_card_present("aaaa")
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    owntone.calls.clear()
    controller.last_error = None


def test_first_card_after_a_restart_leaves_the_selection_alone(ctx):
    """No record of what we set means no way to know whether what OwnTone
    reports is ours or the user's -- and OwnTone's own persisted selection is
    not authoritative (it is only written on a clean shutdown). The safe
    default is not to move the audio anywhere the user did not ask for."""
    controller, owntone, snapshot, _ = ctx
    snapshot.save(["2"])  # a HomePod choice that survived the reboot

    controller.on_card_present("aaaa")

    assert not any(c[0] == "set_outputs" for c in owntone.calls)
    assert owntone.selected_output_ids() == ["1"]
    assert controller.state is State.PLAYING
    # ...and the snapshot is *not* adopted from a selection we cannot vouch
    # for: the saved choice must survive the reboot it was persisted for.
    assert snapshot.load() == ["2"]


def test_user_switching_to_local_while_idle_is_not_clobbered(ctx):
    """The reported bug. The snapshot holds a HomePod; the user goes to
    OwnTone's UI while nothing is playing and picks the local output because
    they want headphones. The next card must not fling the sound back onto the
    HomePod."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["2"])
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["2"]
    controller.on_card_removed()  # record off the platter, nothing playing
    owntone.set_outputs(["1"])    # user picks local in OwnTone's web UI
    owntone.calls.clear()

    controller.on_card_present("bbbb")

    assert not any(c[0] == "set_outputs" for c in owntone.calls)
    assert owntone.selected_output_ids() == ["1"]
    # The new choice becomes the thing we remember for the release cycle.
    assert snapshot.load() == ["1"]


def test_user_switching_to_airplay_while_idle_is_still_respected(ctx):
    """The mirror of the case above, and the one the old guard rail already
    got right. It must keep working."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1"])
    owntone.set_outputs(["1", "2"])  # user adds the HomePod by hand
    owntone.calls.clear()

    controller.on_card_present("aaaa")

    assert not any(c[0] == "set_outputs" for c in owntone.calls)
    assert owntone.selected_output_ids() == ["1", "2"]
    assert snapshot.load() == ["1", "2"]


def test_an_untouched_selection_is_restored_from_the_snapshot(ctx):
    """Nobody has been at the web UI since we selected local at release, so
    the saved choice is still the user's most recent word on the subject."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1", "2"])

    controller.on_card_present("aaaa")

    assert ("set_outputs", ("1", "2")) in owntone.calls
    assert owntone.selected_output_ids() == ["1", "2"]


def test_the_local_fallback_at_card_start_counts_as_ours(ctx):
    """We set the outputs in two places, and both must update the record: a
    fallback that is not remembered looks like a user edit on the next card,
    and the snapshot would never be restored again."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1", "2"])
    real = owntone.set_outputs
    owntone.set_outputs = _boom
    controller.on_card_present("aaaa")  # restore fails; falls back to local
    owntone.set_outputs = real
    owntone.set_outputs(["1"])          # the fallback that did not land
    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    controller.last_error = None
    owntone.calls.clear()

    controller.on_card_present("aaaa")

    assert ("set_outputs", ("1", "2")) in owntone.calls


def test_a_dropped_speaker_is_not_mistaken_for_a_deliberate_change(ctx):
    """A HomePod that deselects itself mid-album is a fault, not a choice. The
    next card must re-select it rather than treat local-only as the user's new
    intent -- and the snapshot must survive."""
    controller, owntone, snapshot, clock = ctx
    _prime(controller, owntone, snapshot, clock)
    snapshot.save(["1", "2"])
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1", "2"]

    owntone.outputs[1]["selected"] = False  # pairing refused, seconds later
    clock.advance(30.0)
    controller.tick()
    assert controller.last_error is not None  # reported, once
    controller.last_error = None

    controller.on_card_removed()
    clock.advance(91.0)
    controller.tick()
    assert snapshot.load() == ["1", "2"]  # the shrink policy holds the line
    controller.last_error = None

    controller.on_card_present("bbbb")

    assert owntone.selected_output_ids() == ["1", "2"]
    assert snapshot.load() == ["1", "2"]
