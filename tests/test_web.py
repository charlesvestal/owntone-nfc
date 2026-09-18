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


def test_status_marks_an_unregistered_card_as_unknown(app_ctx):
    # A bare UID next to a stale dropdown selection reads as if that album is
    # what the card maps to. The page has to be able to say otherwise.
    client, controller, _ = app_ctx
    controller.on_card_present("deadbeef")
    body = client.get("/api/status").get_json()
    assert body["last_seen_uid"] == "deadbeef"
    assert body["last_seen_known"] is False
    assert body["last_seen_name"] is None
    assert body["last_seen_path"] is None


def test_status_resolves_a_registered_card(app_ctx):
    client, controller, store = app_ctx
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Blue",
                                 path="Miles Davis/Kind of Blue")})
    controller.on_card_present("04:A2:B3:C4")
    body = client.get("/api/status").get_json()
    assert body["last_seen_known"] is True
    assert body["last_seen_name"] == "Blue"
    assert body["last_seen_path"] == "Miles Davis/Kind of Blue"


def test_status_with_no_card_seen_reports_unknown_without_crashing(app_ctx):
    client, _, _ = app_ctx
    body = client.get("/api/status").get_json()
    assert body["last_seen_uid"] is None
    assert body["last_seen_known"] is False


def test_learn_mode_sees_unregistered_card(app_ctx):
    client, controller, _ = app_ctx
    controller.on_card_present("deadbeef")
    assert client.get("/api/status").get_json()["last_seen_uid"] == "deadbeef"


def test_post_card_missing_fields_returns_400_not_500(app_ctx):
    client, _, _ = app_ctx
    for payload in ({}, {"uid": "04a2b3c4"},
                    {"uid": "04a2b3c4", "name": "Blue"},
                    {"name": "Blue", "path": "Miles Davis/Kind of Blue"}):
        response = client.post("/api/cards", json=payload)
        assert response.status_code == 400, payload
        assert response.get_json()["error"]


def test_post_card_non_object_body_returns_400(app_ctx):
    client, _, _ = app_ctx
    assert client.post("/api/cards", json=["nope"]).status_code == 400
    response = client.post("/api/cards", data="not json",
                           content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_post_card_blank_uid_returns_400(app_ctx):
    client, _, _ = app_ctx
    response = client.post("/api/cards", json={
        "uid": "::::", "name": "Blue", "path": "Miles Davis/Kind of Blue"})
    assert response.status_code == 400


def test_post_card_rejects_path_that_does_not_exist(app_ctx):
    client, _, store = app_ctx
    response = client.post("/api/cards", json={
        "uid": "04a2b3c4", "name": "Typo", "path": "Miles Davis/Kind of Bleu"})
    assert response.status_code == 400
    # The whole point: a typo must not silently register a card that plays
    # nothing when tapped.
    assert "Kind of Bleu" in response.get_json()["error"]
    assert store.get("04a2b3c4") is None


def test_post_card_rejects_path_escaping_the_library_root(app_ctx):
    client, _, store = app_ctx
    for bad in ("../../etc", "/etc", ""):
        response = client.post("/api/cards", json={
            "uid": "04a2b3c4", "name": "Escape", "path": bad})
        assert response.status_code == 400, bad
    assert store.get("04a2b3c4") is None


def test_post_card_rejects_a_file_that_is_not_a_directory(app_ctx, tmp_path):
    client, _, _ = app_ctx
    (tmp_path / "Miles Davis" / "notes.txt").write_text("hi")
    response = client.post("/api/cards", json={
        "uid": "04a2b3c4", "name": "File", "path": "Miles Davis/notes.txt"})
    assert response.status_code == 400


def test_post_card_does_not_require_a_name(app_ctx):
    # The album path already identifies the record; making the operator retype
    # it as a "name" is redundant. A blank or absent name is derived instead.
    client, _, store = app_ctx
    assert client.post("/api/cards", json={
        "uid": "04a2b3c4", "name": "   ",
        "path": "Miles Davis/Kind of Blue"}).status_code == 201
    assert store.get("04a2b3c4").name == "Kind of Blue"

    assert client.post("/api/cards", json={
        "uid": "04a2b3c5", "path": "Fleetwood Mac/Rumours"}).status_code == 201
    assert store.get("04a2b3c5").name == "Rumours"


def test_post_card_still_honours_an_explicit_name(app_ctx):
    client, _, store = app_ctx
    client.post("/api/cards", json={
        "uid": "04a2b3c6", "name": "Bedtime Songs",
        "path": "Miles Davis/Kind of Blue"})
    assert store.get("04a2b3c6").name == "Bedtime Songs"


def test_concurrent_registrations_do_not_lose_a_card(app_ctx):
    import threading

    client, _, store = app_ctx
    app = client.application
    errors = []

    def register(index):
        try:
            with app.test_client() as worker:
                response = worker.post("/api/cards", json={
                    "uid": f"0000{index:04x}", "name": f"Card {index}",
                    "path": "Miles Davis/Kind of Blue"})
                assert response.status_code == 201
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.load()) == 25


def test_index_renders(app_ctx):
    client, _, _ = app_ctx
    body = client.get("/").get_data(as_text=True)
    assert "Register a card" in body
    # Card names and album folder names are arbitrary user text; building the
    # table with innerHTML would let a folder called <img onerror=...> run
    # script on the admin page.
    assert "innerHTML" not in body.replace(
        "// Everything below builds nodes and sets textContent rather than "
        "innerHTML:", "")
