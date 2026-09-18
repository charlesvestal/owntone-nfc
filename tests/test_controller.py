import httpx
import pytest

from nfc_jukebox.cards import Card
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOwnTone:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.outputs = [
            {"id": "1", "type": "alsa", "selected": True},
            {"id": "2", "type": "airplay", "selected": False},
        ]

    def _ids(self, predicate):
        return [o["id"] for o in self.outputs if predicate(o)]

    def selected_output_ids(self):
        return self._ids(lambda o: o["selected"])

    def airplay_output_ids(self):
        return self._ids(lambda o: o["type"] == "airplay")

    def local_output_ids(self):
        return self._ids(lambda o: o["type"] != "airplay")

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
    assert ("play_album", "Miles Davis/Kind of Blue") == owntone.calls[0][:2]
    assert sum(1 for c in owntone.calls if c[0] == "play_album") == 1


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
    assert owntone.selected_output_ids() == ["1", "2"]


def test_missing_saved_output_falls_back_to_local(ctx):
    controller, owntone, snapshot, _ = ctx
    snapshot.save(["99"])  # HomePod no longer on the network
    controller.on_card_present("aaaa")
    assert owntone.selected_output_ids() == ["1"]
    assert controller.last_error is not None
    assert ("play_album", "Miles Davis/Kind of Blue") in owntone.calls


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


def test_restore_outputs_failure_does_not_escape(ctx):
    controller, owntone, _, _ = ctx
    owntone.selected_output_ids = _boom
    controller.on_card_present("aaaa")
    assert controller.last_error is not None
    assert controller.state is not State.PLAYING


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
