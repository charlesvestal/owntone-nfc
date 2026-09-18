import pytest

from nfc_jukebox.cards import Card, CardStore
from nfc_jukebox.config import Config
from nfc_jukebox.controller import Controller, State
from nfc_jukebox.web import create_app
from tests.test_controller import FakeClock, FakeOwnTone, FakeSnapshot


@pytest.fixture
def app_ctx(tmp_path):
    (tmp_path / "Miles Davis" / "Kind of Blue").mkdir(parents=True)
    (tmp_path / "Fleetwood Mac" / "Rumours").mkdir(parents=True)
    config = Config(library_root=tmp_path, cards_file=tmp_path / "cards.yaml")
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)
    return app.test_client(), controller, store


def test_status_reports_state(app_ctx):
    client, controller, _ = app_ctx
    controller.state = State.IDLE
    body = client.get("/api/status").get_json()
    assert body["state"] == "idle"
    assert "last_seen_uid" in body


def test_albums_are_listed_relative_to_library_root(app_ctx):
    client, _, _ = app_ctx
    albums = client.get("/api/albums").get_json()["albums"]
    assert "Miles Davis/Kind of Blue" in albums
    assert "Fleetwood Mac/Rumours" in albums


def test_post_card_saves_mapping(app_ctx):
    client, _, store = app_ctx
    response = client.post("/api/cards", json={
        "uid": "04:A2:B3:C4", "name": "Blue", "path": "Miles Davis/Kind of Blue"})
    assert response.status_code == 201
    assert store.get("04a2b3c4").path == "Miles Davis/Kind of Blue"


def test_delete_card_removes_mapping(app_ctx):
    client, _, store = app_ctx
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Blue", path="A/B")})
    assert client.delete("/api/cards/04a2b3c4").status_code == 204
    assert store.get("04a2b3c4") is None


def test_learn_mode_sees_unregistered_card(app_ctx):
    client, controller, _ = app_ctx
    controller.on_card_present("deadbeef")
    assert client.get("/api/status").get_json()["last_seen_uid"] == "deadbeef"
