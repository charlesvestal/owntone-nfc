import json

import httpx
import pytest
import respx

from nfc_jukebox.owntone import ALBUM_EXPRESSION, OwnTone

BASE = "http://test:3689"

# The `type` strings are OwnTone's own: the `.name` field of each
# `struct output_definition` in src/outputs/*.c. Spelling them the way the
# server really spells them is the whole point of this fixture -- the previous
# lowercase guesses ("airplay", "alsa") are what hid the release bug.
OUTPUTS = {
    "outputs": [
        {"id": "1", "name": "Local", "type": "ALSA", "selected": True},
        {"id": "2", "name": "Kitchen", "type": "AirPlay 2", "selected": False},
        {"id": "3", "name": "Lounge", "type": "AirPlay 1", "selected": True},
        {"id": "4", "name": "Desk", "type": "Pulseaudio", "selected": False},
        {"id": "5", "name": "Telly", "type": "Chromecast", "selected": False},
        {"id": "6", "name": "HTTP", "type": "streaming", "selected": False},
    ]
}


@pytest.fixture
def client():
    return OwnTone(BASE)


@respx.mock
def test_selected_output_ids(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.selected_output_ids() == ["1", "3"]


@respx.mock
def test_airplay_covers_both_airplay_1_and_2(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    # Both raop.c ("AirPlay 1") and airplay.c ("AirPlay 2") are HomePod-shaped
    # sessions that have to be handed back when the record comes off.
    assert client.airplay_output_ids() == ["2", "3"]


@respx.mock
def test_airplay_detection_is_case_insensitive(client):
    respx.get(f"{BASE}/api/outputs").mock(
        return_value=httpx.Response(200, json={"outputs": [
            {"id": "9", "type": "airplay", "selected": False},
            {"id": "8", "type": "AIRPLAY 2", "selected": False},
        ]})
    )
    assert client.airplay_output_ids() == ["9", "8"]


@respx.mock
def test_local_outputs_are_an_allow_list_not_everything_else(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    # Only genuine soundcards. A release must not go and light up the
    # neighbour's Chromecast or OwnTone's HTTP streaming endpoint.
    assert client.local_output_ids() == ["1", "4"]


@respx.mock
def test_all_output_ids(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.all_output_ids() == ["1", "2", "3", "4", "5", "6"]


@respx.mock
def test_set_outputs_sends_expected_body(client):
    route = respx.put(f"{BASE}/api/outputs/set").mock(return_value=httpx.Response(204))
    client.set_outputs(["2", "3"])
    assert route.called
    # Assert the decoded payload, not byte-level formatting: how the JSON
    # serialiser spaces its separators is not part of the contract.
    assert json.loads(respx.calls.last.request.read()) == {"outputs": ["2", "3"]}


def test_album_expression_orders_by_the_real_lexer_tags():
    # Verified against a live OwnTone 29.3.142 server.
    # `track_number`/`disc_number` are JSON-API field names and do not parse.
    assert "track_number" not in ALBUM_EXPRESSION
    assert "disc_number" not in ALBUM_EXPRESSION


def test_album_expression_orders_by_exactly_one_field():
    # Multi-field ordering is a syntax error on the server: `order by disc asc,
    # track asc` returns 500, because the comma is rejected. Without ANY
    # ordering the server returns tracks effectively shuffled, so exactly one
    # sort field is required - not zero, not two.
    assert "order by" in ALBUM_EXPRESSION
    order_clause = ALBUM_EXPRESSION.split("order by", 1)[1]
    assert "," not in order_clause


@respx.mock
def test_play_album_anchors_the_path_so_siblings_do_not_match(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 11})
    )
    client.play_album("Fleetwood Mac/Rumours")
    expression = respx.calls.last.request.url.params["expression"]
    # A trailing separator anchors the prefix at a directory boundary, so a
    # sibling `Rumours (Deluxe)` folder cannot also be swept into the queue and
    # interleaved track-for-track with the album the user actually asked for.
    assert expression.startswith('path includes "Fleetwood Mac/Rumours/"')
    needle = "Fleetwood Mac/Rumours/"
    assert needle not in "/srv/music/Fleetwood Mac/Rumours (Deluxe)/01.flac"
    assert needle not in "/srv/music/Fleetwood Mac/Rumours - 2013 Remaster/01.flac"
    assert needle in "/srv/music/Fleetwood Mac/Rumours/01.flac"


@respx.mock
def test_play_album_does_not_double_the_separator(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 11})
    )
    client.play_album("Fleetwood Mac/Rumours/")
    expression = respx.calls.last.request.url.params["expression"]
    assert expression.startswith('path includes "Fleetwood Mac/Rumours/"')


@respx.mock
def test_shuffle_and_repeat_can_be_forced_off(client):
    shuffle = respx.put(url__startswith=f"{BASE}/api/player/shuffle").mock(
        return_value=httpx.Response(204)
    )
    repeat = respx.put(url__startswith=f"{BASE}/api/player/repeat").mock(
        return_value=httpx.Response(204)
    )
    client.shuffle(False)
    client.repeat("off")
    assert shuffle.called and repeat.called
    assert dict(shuffle.calls.last.request.url.params) == {"state": "false"}
    assert dict(repeat.calls.last.request.url.params) == {"state": "off"}


@respx.mock
def test_set_vinyl_playback_mode_turns_both_off(client):
    shuffle = respx.put(url__startswith=f"{BASE}/api/player/shuffle").mock(
        return_value=httpx.Response(204)
    )
    repeat = respx.put(url__startswith=f"{BASE}/api/player/repeat").mock(
        return_value=httpx.Response(204)
    )
    client.set_vinyl_playback_mode()
    assert dict(shuffle.calls.last.request.url.params) == {"state": "false"}
    assert dict(repeat.calls.last.request.url.params) == {"state": "off"}


@respx.mock
def test_play_album_clears_and_starts(client):
    route = respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 9})
    )
    client.play_album("Miles Davis/Kind of Blue")
    assert route.called
    url = str(respx.calls.last.request.url)
    assert "clear=true" in url
    assert "playback=start" in url
    assert "Kind+of+Blue" in url or "Kind%20of%20Blue" in url


@respx.mock
def test_pause_and_stop(client):
    pause = respx.put(f"{BASE}/api/player/pause").mock(return_value=httpx.Response(204))
    stop = respx.put(f"{BASE}/api/player/stop").mock(return_value=httpx.Response(204))
    client.pause()
    client.stop()
    assert pause.called and stop.called


@respx.mock
def test_clear_queue(client):
    route = respx.put(f"{BASE}/api/queue/clear").mock(return_value=httpx.Response(204))
    client.clear_queue()
    assert route.called


@respx.mock
def test_raises_on_server_error(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client.selected_output_ids()


@respx.mock
def test_play_album_escapes_double_quotes_in_path(client):
    route = respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 3})
    )
    client.play_album('Various/12" Singles')
    assert route.called
    expression = respx.calls.last.request.url.params["expression"]
    # The interpolated value must not terminate the quoted string early.
    assert expression == ALBUM_EXPRESSION.format(path='Various/12\\" Singles/')
    assert expression.startswith('path includes "Various/12\\" Singles/"')


@respx.mock
def test_play_album_escapes_backslashes_in_path(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 3})
    )
    client.play_album("Some\\Path")
    expression = respx.calls.last.request.url.params["expression"]
    assert expression.startswith('path includes "Some\\\\Path/"')


@respx.mock
def test_play_album_leaves_single_quotes_alone(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 3})
    )
    client.play_album("Led Zeppelin/Rock 'n' Roll")
    expression = respx.calls.last.request.url.params["expression"]
    assert expression.startswith('path includes "Led Zeppelin/Rock \'n\' Roll/"')


@respx.mock
def test_album_artwork_url_is_returned_rooted(client):
    respx.get(url__startswith=f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={
            "albums": {"items": [{"name": "Parklife",
                                  "artwork_url": "./artwork/group/7"}]}}))
    # OwnTone returns "./artwork/group/7"; a browser would resolve that against
    # the current page, so it must come back rooted.
    assert client.album_artwork_url("Blur/Parklife") == "/artwork/group/7"


@respx.mock
def test_album_artwork_url_is_none_when_no_album_matches(client):
    respx.get(url__startswith=f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={"albums": {"items": []}}))
    assert client.album_artwork_url("Nope/Nothing") is None


@respx.mock
def test_album_artwork_url_is_none_when_album_has_no_art(client):
    respx.get(url__startswith=f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={
            "albums": {"items": [{"name": "Parklife"}]}}))
    assert client.album_artwork_url("Blur/Parklife") is None
