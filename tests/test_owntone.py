import json

import httpx
import pytest
import respx

from nfc_jukebox.owntone import ALBUM_EXPRESSION, OwnTone

BASE = "http://test:3689"

OUTPUTS = {
    "outputs": [
        {"id": "1", "name": "Local", "type": "alsa", "selected": True},
        {"id": "2", "name": "Kitchen", "type": "airplay", "selected": False},
        {"id": "3", "name": "Lounge", "type": "airplay", "selected": True},
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
def test_airplay_and_local_partition(client):
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.airplay_output_ids() == ["2", "3"]
    respx.get(f"{BASE}/api/outputs").mock(return_value=httpx.Response(200, json=OUTPUTS))
    assert client.local_output_ids() == ["1"]


@respx.mock
def test_set_outputs_sends_expected_body(client):
    route = respx.put(f"{BASE}/api/outputs/set").mock(return_value=httpx.Response(204))
    client.set_outputs(["2", "3"])
    assert route.called
    # Assert the decoded payload, not byte-level formatting: how the JSON
    # serialiser spaces its separators is not part of the contract.
    assert json.loads(respx.calls.last.request.read()) == {"outputs": ["2", "3"]}


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
    assert expression == ALBUM_EXPRESSION.format(path='Various/12\\" Singles')
    assert expression.startswith('path includes "Various/12\\" Singles"')


@respx.mock
def test_play_album_escapes_backslashes_in_path(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 3})
    )
    client.play_album("Some\\Path")
    expression = respx.calls.last.request.url.params["expression"]
    assert expression.startswith('path includes "Some\\\\Path"')


@respx.mock
def test_play_album_leaves_single_quotes_alone(client):
    respx.post(url__startswith=f"{BASE}/api/queue/items/add").mock(
        return_value=httpx.Response(200, json={"count": 3})
    )
    client.play_album("Led Zeppelin/Rock 'n' Roll")
    expression = respx.calls.last.request.url.params["expression"]
    assert expression.startswith('path includes "Led Zeppelin/Rock \'n\' Roll"')
