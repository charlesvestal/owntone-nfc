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


def test_album_expression_carries_no_ordering():
    # Ordering is done client-side in play_album, because OwnTone accepts
    # exactly one sort field and a multi-disc album needs two: `order by disc
    # asc, track asc` is a syntax error on the server.
    assert "order by" not in ALBUM_EXPRESSION
    assert "track_number" not in ALBUM_EXPRESSION
    assert "disc_number" not in ALBUM_EXPRESSION


def _track(uri, disc, track, path="x"):
    return {"uri": uri, "disc_number": disc, "track_number": track, "path": path}


def _mock_album(tracks):
    """Mock the track search play_album performs, and the queue add."""
    respx.get(url__startswith=f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": tracks}}))
    return respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": len(tracks)}))


@respx.mock
def test_play_album_anchors_the_path_so_siblings_do_not_match(client):
    _mock_album([_track("library:track:1", 1, 1)])
    client.play_album("Fleetwood Mac/Rumours")
    # A card for "Rumours" must not also sweep in "Rumours (Deluxe)".
    expression = respx.calls[0].request.url.params["expression"]
    assert 'path includes "Fleetwood Mac/Rumours/"' == expression


@respx.mock
def test_play_album_does_not_double_the_separator(client):
    _mock_album([_track("library:track:1", 1, 1)])
    client.play_album("Fleetwood Mac/Rumours/")
    assert 'path includes "Fleetwood Mac/Rumours/"' == \
        respx.calls[0].request.url.params["expression"]


@respx.mock
def test_play_album_clears_and_starts(client):
    route = _mock_album([_track("library:track:1", 1, 1)])
    client.play_album("Miles Davis/Kind of Blue")
    assert route.called
    params = respx.calls.last.request.url.params
    assert params["clear"] == "true"
    assert params["playback"] == "start"


@respx.mock
def test_play_album_orders_a_multi_disc_album_by_disc_then_track(client):
    # The case this exists for: M83's "Hurry Up, We're Dreaming" is two discs
    # in one folder, so both have a track 1. Sorting by track or by path
    # interleaves them; only (disc, track) is correct. OwnTone cannot do it -
    # its expression grammar takes exactly one sort field.
    _mock_album([
        _track("library:track:d2t2", 2, 2),
        _track("library:track:d1t1", 1, 1),
        _track("library:track:d2t1", 2, 1),
        _track("library:track:d1t2", 1, 2),
    ])
    client.play_album("M83/Hurry Up, We're Dreaming")
    uris = respx.calls.last.request.url.params["uris"].split(",")
    assert uris == ["library:track:d1t1", "library:track:d1t2",
                    "library:track:d2t1", "library:track:d2t2"]


@respx.mock
def test_play_album_falls_back_to_path_when_tags_are_missing(client):
    # Untagged rips sort by path rather than landing in arbitrary order.
    _mock_album([
        {"uri": "library:track:b", "path": "02 b.flac"},
        {"uri": "library:track:a", "path": "01 a.flac"},
    ])
    client.play_album("Some/Album")
    assert respx.calls.last.request.url.params["uris"].split(",") == \
        ["library:track:a", "library:track:b"]


@respx.mock
def test_play_album_raises_when_nothing_matches(client):
    _mock_album([])
    # Queueing an empty album silently would look like a dead card.
    with pytest.raises(LookupError):
        client.play_album("Nope/Nothing")


@respx.mock
def test_play_album_escapes_double_quotes_in_path(client):
    _mock_album([_track('library:track:1', 1, 1)])
    # An unescaped quote would close the expression string early and the
    # whole query would fail to parse.
    client.play_album('Weird/Album "Name"')
    expression = respx.calls[0].request.url.params['expression']
    assert '\\"Name\\"' in expression


@respx.mock
def test_play_album_escapes_backslashes_in_path(client):
    _mock_album([_track('library:track:1', 1, 1)])
    client.play_album('Weird/Back\\slash')
    expression = respx.calls[0].request.url.params['expression']
    assert 'Back\\\\slash' in expression


@respx.mock
def test_play_album_leaves_single_quotes_alone(client):
    _mock_album([_track("library:track:1", 1, 1)])
    client.play_album("Rock 'n' Roll/Album")
    assert "Rock 'n' Roll" in respx.calls[0].request.url.params["expression"]


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


@respx.mock
def test_album_artwork_url_escapes_the_path_too(client):
    # The same escaping bug appeared twice in one sitting: once in play_album
    # and once here. Both build the expression, so both must escape it.
    respx.get(url__startswith=f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={"albums": {"items": []}}))
    client.album_artwork_url('Weird/Album "Name"')
    expression = respx.calls.last.request.url.params["expression"]
    assert '\\"Name\\"' in expression
