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
    body = client.get("/api/albums").get_json()
    paths = [a["path"] for a in body["albums"]]
    assert "Miles Davis/Kind of Blue" in paths
    assert "Fleetwood Mac/Rumours" in paths


def test_albums_report_which_ones_already_have_a_card(app_ctx):
    # The library is far larger than the number of cards, so "what still needs
    # one" is the question being asked while registering - and a second card
    # on an album already done is otherwise invisible until you tap it.
    client, _, store = app_ctx
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Blue",
                                 path="Miles Davis/Kind of Blue")})
    body = client.get("/api/albums").get_json()
    by_path = {a["path"]: a for a in body["albums"]}

    assert by_path["Miles Davis/Kind of Blue"]["assigned"] is True
    assert by_path["Miles Davis/Kind of Blue"]["card_name"] == "Blue"
    assert by_path["Fleetwood Mac/Rumours"]["assigned"] is False
    assert by_path["Fleetwood Mac/Rumours"]["card_name"] is None

    assert body["total"] == 2
    assert body["unassigned"] == 1


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


def test_every_element_the_page_scripts_reference_actually_exists():
    """Guards against half-applied template edits.

    A change once added `getElementById('known')` to the script without adding
    the matching element, so refresh() threw on its first run and the card
    table never rendered - which looked exactly like "all my registrations
    disappeared". Nothing caught it: no test renders the page, let alone runs
    its JavaScript.

    This is not a substitute for executing the page, but it makes the specific
    silent failure - script and markup drifting apart - impossible to ship.
    """
    import re
    from pathlib import Path

    import nfc_jukebox

    html = (Path(nfc_jukebox.__file__).parent / "templates" / "index.html").read_text()
    referenced = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", html))
    assert referenced, "expected the page to reference some elements by id"
    defined = set(re.findall(r"""\bid=["']([^"']+)["']""", html))
    missing = sorted(referenced - defined)
    assert not missing, f"script references ids with no matching element: {missing}"


def test_management_mode_toggles_through_the_api(app_ctx):
    client, controller, _ = app_ctx
    assert client.get("/api/status").get_json()["management_mode"] is False

    assert client.put("/api/management", json={"enabled": True}).status_code == 200
    assert controller.management_mode is True
    assert client.get("/api/status").get_json()["management_mode"] is True

    client.put("/api/management", json={"enabled": False})
    assert controller.management_mode is False


def test_management_mode_rejects_a_non_boolean(app_ctx):
    client, controller, _ = app_ctx
    assert client.put("/api/management", json={"enabled": "yes"}).status_code == 400
    assert controller.management_mode is False


def test_artwork_requires_a_path(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/api/artwork").status_code == 400


def test_artwork_returns_null_when_no_owntone_client(app_ctx):
    # create_app tolerates being built without one; the page must not break.
    client, _, _ = app_ctx
    assert client.get("/api/artwork?path=A/B").get_json()["url"] is None


def test_status_reports_where_sound_will_come_out(app_ctx, monkeypatch):
    # The page has to answer "what happens if I tap a card right now" without
    # opening OwnTone. Today the answer lived in three places that disagreed.
    from nfc_jukebox import web as web_module

    class FakeOwnToneOutputs:
        def __init__(self):
            self.calls = 0

        def outputs(self):
            self.calls += 1
            return [{"name": "Computer", "selected": True},
                    {"name": "HomePod Left", "selected": False}]

    _, controller, store = app_ctx
    fake = FakeOwnToneOutputs()
    app = web_module.create_app(Config(), controller, store, owntone=fake)
    app.config.update(TESTING=True)
    client = app.test_client()

    assert client.get("/api/status").get_json()["outputs"] == ["Computer"]

    # Polled once a second; the lookup must be cached, not asked every time.
    for _ in range(5):
        client.get("/api/status")
    assert fake.calls == 1


def test_status_outputs_is_null_without_an_owntone_client(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/api/status").get_json()["outputs"] is None


def test_status_survives_owntone_being_unreachable(app_ctx):
    from nfc_jukebox import web as web_module

    class Broken:
        def outputs(self):
            raise RuntimeError("owntone is down")

    app = web_module.create_app(Config(), app_ctx[1], app_ctx[2], owntone=Broken())
    app.config.update(TESTING=True)
    # A dead OwnTone must not take the admin page down with it.
    body = app.test_client().get("/api/status").get_json()
    assert body["outputs"] is None
    assert body["state"] is not None


def _power_app(app_ctx, recorder):
    from nfc_jukebox import web as web_module
    _, controller, store = app_ctx
    app = web_module.create_app(Config(), controller, store, power=recorder)
    app.config.update(TESTING=True)
    return app.test_client()


def test_power_off_and_reboot_are_dispatched(app_ctx):
    seen = []
    client = _power_app(app_ctx, seen.append)
    assert client.post("/api/power", json={"action": "poweroff"}).status_code == 200
    assert client.post("/api/power", json={"action": "reboot"}).status_code == 200
    assert seen == ["poweroff", "reboot"]


def test_power_rejects_anything_else(app_ctx):
    seen = []
    client = _power_app(app_ctx, seen.append)
    for bad in ({"action": "rm -rf /"}, {"action": "halt"}, {}, {"action": None}):
        assert client.post("/api/power", json=bad).status_code == 400
    # Nothing unrecognised is ever passed through to the shell.
    assert seen == []


def test_power_reports_a_failure_instead_of_pretending(app_ctx):
    def boom(action):
        raise OSError("sudo: a password is required")
    client = _power_app(app_ctx, boom)
    response = client.post("/api/power", json={"action": "poweroff"})
    assert response.status_code == 500
    assert "password" in response.get_json()["error"]
