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
    controller, owntone, snapshot, _ = ctx
    snapshot.save(["1"])
    owntone.outputs[1]["selected"] = True  # user picked AirPlay by hand
    controller.on_card_present("aaaa")
    # Assert the *absence of the write*, not the resulting state: the state was
    # set up two lines above, so asserting it would pass even if
    # _restore_outputs were replaced with `return`.
    assert not any(c[0] == "set_outputs" for c in owntone.calls)
    assert owntone.selected_output_ids() == ["1", "2"]


def test_manual_chromecast_selection_while_idle_is_not_clobbered(ctx):
    """Neither local nor AirPlay is still a deliberate choice."""
    controller, owntone, snapshot, _ = ctx
    owntone.outputs.append({"id": "7", "type": "Chromecast", "selected": True})
    owntone.outputs[0]["selected"] = False
    snapshot.save(["1"])
    controller.on_card_present("aaaa")
    assert not any(c[0] == "set_outputs" for c in owntone.calls)


def test_missing_saved_output_falls_back_to_local(ctx):
    controller, owntone, snapshot, _ = ctx
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
