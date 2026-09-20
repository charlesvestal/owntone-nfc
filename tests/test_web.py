import json
import threading
import time
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


# --- start over -------------------------------------------------------------
#
# Lifting a card now pauses rather than resets, so the only way back to track 1
# mid-listen is to ask for it. This is the one transport control the admin page
# carries, and it exists because the vinyl model took the other one away.


def _loaded(app_ctx):
    """An app whose controller has an album loaded, as after a tap."""
    client, controller, store = app_ctx
    store.save({"aaaa": Card(uid="aaaa", name="Blue",
                             path="Miles Davis/Kind of Blue")})
    controller.on_card_present("aaaa")
    return client, controller


def test_start_over_restarts_the_loaded_album(app_ctx):
    client, controller = _loaded(app_ctx)
    response = client.post("/api/startover")
    assert response.status_code == 200
    assert response.get_json()["now_playing"] == "Blue"
    assert controller.state is State.PLAYING


def test_start_over_with_nothing_loaded_is_a_409_not_a_500(app_ctx):
    client, _, _ = app_ctx
    response = client.post("/api/startover")
    assert response.status_code == 409
    assert response.get_json()["error"]


def test_start_over_surfaces_an_owntone_failure(app_ctx):
    client, controller, _ = app_ctx

    def boom():
        raise RuntimeError("OwnTone error while starting Blue over: 500")

    controller.start_over = boom
    response = client.post("/api/startover")
    assert response.status_code == 500
    assert "500" in response.get_json()["error"]


def test_the_page_offers_start_over(app_ctx):
    client, _, _ = app_ctx
    assert "Start over" in client.get("/").get_data(as_text=True)


# --- library availability --------------------------------------------------
#
# The jukebox now starts even when the music is not mounted, so that a NAS
# that fails to resolve leaves an admin page explaining itself rather than a
# silent box. That is only worth anything if the page actually says so.


def test_status_reports_a_healthy_library(app_ctx):
    client, _, _ = app_ctx
    body = client.get("/api/status").get_json()
    assert body["library_ok"] is True
    assert body["library_error"] is None


def test_status_reports_a_library_that_is_not_there(tmp_path):
    """The observed failure: the CIFS mount did not happen, so the mount point
    is an empty directory or missing altogether."""
    config = Config(library_root=tmp_path / "nothing-here",
                    cards_file=tmp_path / "cards.yaml")
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)

    body = app.test_client().get("/api/status").get_json()

    assert body["library_ok"] is False
    assert "not mounted" in body["library_error"].lower()


def test_an_empty_library_root_counts_as_not_mounted(tmp_path):
    """An automount point with nothing under it looks exactly like a mount
    that failed, because that is what it is."""
    root = tmp_path / "srv-music"
    root.mkdir()
    config = Config(library_root=root, cards_file=tmp_path / "cards.yaml")
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)

    body = app.test_client().get("/api/status").get_json()

    assert body["library_ok"] is False


def test_a_broken_mount_is_reported_rather_than_raising(app_ctx, monkeypatch):
    """A dead CIFS mount does not raise FileNotFoundError -- it raises OSError
    ENODEV ("No such device"), which is what /srv/music actually did. An
    unhandled one here would take the whole status endpoint down, and with it
    the page that is supposed to explain the problem."""
    import nfc_jukebox.web as web_module

    client, _, _ = app_ctx

    def boom(_path):
        raise OSError(19, "No such device")

    monkeypatch.setattr(web_module.os, "listdir", boom)
    body = client.get("/api/status").get_json()

    assert body["library_ok"] is False
    assert "No such device" in body["library_error"]


# --- artwork studio ----------------------------------------------------
#
# Collecting artwork, judging what came back and printing the sheets are all
# part of making a card, so they belong on the page that makes cards. The
# fetching itself is injected: it talks to the internet, takes minutes, and
# none of that belongs in a test.


def _offline_collector(album_path, out_dir, library_root, overrides):
    """Stands in for the real fetcher so no test ever reaches the network.

    Tests that care what the collector did pass their own.
    """
    return {"status": "missing"}


def _studio(tmp_path, collector=None, manifest=None):
    (tmp_path / "Miles Davis" / "Kind of Blue").mkdir(parents=True)
    art = tmp_path / "art"
    art.mkdir()
    if manifest is not None:
        # Give every album in the manifest a real folder unless the test is
        # deliberately testing a deleted one: the manifest is pruned of albums
        # that no longer exist, so a row with no folder vanishes.
        for album in manifest:
            if not album.startswith("Gone/"):
                (tmp_path / album).mkdir(parents=True, exist_ok=True)
        (art / "manifest.json").write_text(json.dumps(manifest))
    config = Config(library_root=tmp_path, cards_file=tmp_path / "cards.yaml",
                    artwork_dir=art)
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store,
                     collector=collector or _offline_collector)
    app.config.update(TESTING=True)
    return app.test_client(), art


def test_albums_are_listed_as_pending_before_anything_is_collected(tmp_path):
    """The grid shows the whole job from the start, not just what is done.

    This used to assert an empty list: the grid was built from the manifest, so
    a box that had never collected showed nothing at all and a run made albums
    appear one at a time.
    """
    client, _ = _studio(tmp_path)
    body = client.get("/api/artwork/manifest").get_json()
    assert [(a["album"], a["verdict"]) for a in body["albums"]] == [
        ("Miles Davis/Kind of Blue", "pending")]
    assert body["running"] is False


def test_artwork_manifest_reports_what_was_collected(tmp_path):
    client, _ = _studio(tmp_path, manifest={
        "Miles Davis/Kind of Blue": {
            "status": "ok", "file": "x.jpg", "width": 1400, "height": 1400,
            "source": "itunes:Miles Davis - Kind of Blue", "score": 1.0},
    })
    body = client.get("/api/artwork/manifest").get_json()
    assert len(body["albums"]) == 1
    entry = body["albums"][0]
    assert entry["album"] == "Miles Davis/Kind of Blue"
    assert entry["verdict"] == "fetched"


def test_a_questionable_match_is_flagged_for_review(tmp_path):
    """The whole point of the review step: a search returned something, and it
    looks like a different record."""
    client, _ = _studio(tmp_path, manifest={
        "Liquid Mike/S_T": {
            "status": "ok", "file": "x.jpg", "width": 3000, "height": 3000,
            "source": "itunes:Liquid Mike - Paul Bunyan's Slingshot",
            "score": 0.37},
    })
    body = client.get("/api/artwork/manifest").get_json()
    assert body["albums"][0]["verdict"] == "suspect"


def test_collecting_runs_in_the_background_and_reports_progress(tmp_path):
    """A run over a whole library takes minutes, so the request must not wait
    for it -- the page polls instead."""
    seen = []

    def collector(album_path, out_dir, library_root, overrides):
        seen.append(album_path)
        return {"status": "ok", "file": "x.jpg", "width": 1200,
                "height": 1200, "source": "itunes:x", "score": 1.0}

    client, art = _studio(tmp_path, collector=collector)
    started = client.post("/api/artwork/collect").get_json()
    assert started["started"] is True

    for _ in range(200):
        body = client.get("/api/artwork/manifest").get_json()
        if not body["running"]:
            break
        time.sleep(0.01)

    assert seen == ["Miles Davis/Kind of Blue"]
    assert len(body["albums"]) == 1
    assert json.loads((art / "manifest.json").read_text())


def test_collecting_twice_at_once_is_refused_rather_than_doubled(tmp_path):
    release = threading.Event()

    def collector(album_path, out_dir, library_root, overrides):
        release.wait(2)
        return {"status": "missing"}

    client, _ = _studio(tmp_path, collector=collector)
    assert client.post("/api/artwork/collect").get_json()["started"] is True
    second = client.post("/api/artwork/collect")
    assert second.status_code == 409
    release.set()


def test_a_failing_collector_does_not_kill_the_run(tmp_path):
    """One album that raises must not leave the page thinking a run is still
    in progress for ever."""
    def collector(album_path, out_dir, library_root, overrides):
        raise RuntimeError("iTunes fell over")

    client, _ = _studio(tmp_path, collector=collector)
    client.post("/api/artwork/collect")
    for _ in range(200):
        body = client.get("/api/artwork/manifest").get_json()
        if not body["running"]:
            break
        time.sleep(0.01)
    assert body["running"] is False
    assert body["albums"][0]["status"] == "missing"


def test_an_override_is_saved_and_shows_on_the_entry(tmp_path):
    client, art = _studio(tmp_path)
    response = client.put("/api/artwork/override",
                          json={"album": "Liquid Mike/S_T",
                                "album_name": "Liquid Mike"})
    assert response.status_code == 200
    saved = json.loads((art / "overrides.json").read_text())
    assert saved["Liquid Mike/S_T"]["album"] == "Liquid Mike"


def test_an_override_can_skip_an_album_entirely(tmp_path):
    client, art = _studio(tmp_path)
    client.put("/api/artwork/override",
               json={"album": "ZZ Test/Stereo Test", "skip": "test disc"})
    saved = json.loads((art / "overrides.json").read_text())
    assert saved["ZZ Test/Stereo Test"]["skip"] == "test disc"


def test_clearing_an_override_removes_it(tmp_path):
    client, art = _studio(tmp_path)
    client.put("/api/artwork/override", json={"album": "A/B", "skip": "x"})
    client.put("/api/artwork/override", json={"album": "A/B"})
    assert json.loads((art / "overrides.json").read_text()) == {}


def test_an_override_without_an_album_is_a_400(tmp_path):
    client, _ = _studio(tmp_path)
    assert client.put("/api/artwork/override", json={"skip": "x"}).status_code == 400


def test_artwork_images_are_served_from_the_artwork_directory(tmp_path):
    client, art = _studio(tmp_path, manifest={
        "A/B": {"status": "ok", "file": "pic.jpg", "width": 1200,
                "height": 1200, "source": "itunes:x", "score": 1.0}})
    (art / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0not-really-a-jpeg")
    response = client.get("/artwork-file/pic.jpg")
    assert response.status_code == 200
    assert response.data.startswith(b"\xff\xd8")


def test_artwork_file_paths_cannot_escape_the_directory(tmp_path):
    """The name comes from a URL, so it is attacker-controlled by definition.

    Following redirects on purpose: Flask normalises a doubled slash with a
    308 before the handler ever sees it, and what matters is where you end up,
    not that the first hop was a refusal.
    """
    client, _ = _studio(tmp_path)
    (tmp_path / "cards.yaml").write_text("secret: yes")
    for name in ("../cards.yaml", "..%2Fcards.yaml", "/etc/passwd",
                 "....//cards.yaml"):
        response = client.get(f"/artwork-file/{name}", follow_redirects=True)
        assert response.status_code in (400, 404), name
        assert b"secret" not in response.data


# --- duplicate cards -------------------------------------------------------


def _register(client, uid, path):
    return client.post("/api/cards", json={"uid": uid, "path": path})


def test_cards_are_not_flagged_when_every_album_has_one_card(app_ctx):
    client, _, _ = app_ctx
    _register(client, "aa", "Miles Davis/Kind of Blue")
    _register(client, "bb", "Fleetwood Mac/Rumours")
    cards = client.get("/api/cards").get_json()["cards"]
    assert [c["duplicate"] for c in cards] == [False, False]


def test_both_cards_on_one_album_are_flagged_as_duplicates(app_ctx):
    client, _, _ = app_ctx
    _register(client, "aa", "Miles Davis/Kind of Blue")
    _register(client, "bb", "Miles Davis/Kind of Blue")
    _register(client, "cc", "Fleetwood Mac/Rumours")
    flagged = {c["uid"]: c["duplicate"]
               for c in client.get("/api/cards").get_json()["cards"]}
    assert flagged == {"aa": True, "bb": True, "cc": False}


# --- artwork caching -------------------------------------------------------
#
# Artwork is replaced in place under a stable filename, so a long max-age with
# no version in the URL served a stale image for a day. The manifest carries
# each file's mtime and the page versions the URL with it, which lets the
# response be cached forever and still update the moment the bytes change.


@pytest.fixture
def art_ctx(tmp_path):
    """An app whose artwork directory is a real directory we can write."""
    # The album has to exist on disk: the manifest is pruned of albums whose
    # folder has gone, so a fixture without one collects nothing.
    (tmp_path / "Miles Davis" / "Kind of Blue").mkdir(parents=True)
    art = tmp_path / "artwork"
    art.mkdir()
    config = Config(library_root=tmp_path, cards_file=tmp_path / "cards.yaml",
                    artwork_dir=art)
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)
    return app.test_client(), art


def _manifest(art, entry):
    import json
    (art / "manifest.json").write_text(
        json.dumps({"Miles Davis/Kind of Blue": entry}))


def test_a_collected_file_carries_its_mtime(art_ctx):
    client, art = art_ctx
    (art / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    _manifest(art, {"status": "ok", "file": "cover.jpg",
                    "width": 3000, "height": 3000})
    row = client.get("/api/artwork/manifest").get_json()["albums"][0]
    assert row["mtime"] == pytest.approx((art / "cover.jpg").stat().st_mtime)


def test_a_missing_file_has_no_mtime(art_ctx):
    client, art = art_ctx
    _manifest(art, {"status": "missing"})
    row = client.get("/api/artwork/manifest").get_json()["albums"][0]
    assert row["mtime"] is None


def test_artwork_is_cached_forever_and_immutable(art_ctx):
    client, art = art_ctx
    (art / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    cache = client.get("/artwork-file/cover.jpg").headers["Cache-Control"]
    assert "immutable" in cache
    assert "max-age=31536000" in cache


def test_a_version_parameter_does_not_defeat_the_traversal_check(art_ctx):
    client, _ = art_ctx
    assert client.get("/artwork-file/../../etc/passwd?v=1").status_code in (400, 404)


# --- overrides apply immediately -------------------------------------------
#
# Setting an override used to record the intent and stop there, leaving the
# page showing the old art until a separate Collect run. That reads as "it
# didn't work", so the override now collects that one album on the spot.


def test_pinning_a_url_collects_that_album_at_once(tmp_path):
    seen = []

    def collector(album_path, out_dir, library_root, overrides):
        seen.append((album_path, overrides.get(album_path)))
        return {"status": "ok", "file": "cover.jpg", "width": 1425,
                "height": 1425, "source": "override:http://x/y.jpg",
                "score": 1.0, "override": True}

    client, art = _studio(tmp_path, collector=collector)
    body = client.put("/api/artwork/override",
                      json={"album": "A/B", "url": "http://x/y.jpg"}).get_json()

    assert seen == [("A/B", {"url": "http://x/y.jpg"})]
    assert body["collected"] is True
    assert body["entry"]["source"] == "override:http://x/y.jpg"
    # ...and it is persisted, not merely returned.
    import json
    saved = json.loads((art / "manifest.json").read_text())
    assert saved["A/B"]["source"] == "override:http://x/y.jpg"


def test_clearing_an_override_re_collects(tmp_path):
    seen = []

    def collector(album_path, out_dir, library_root, overrides):
        seen.append(overrides.get(album_path))
        return {"status": "ok", "file": "c.jpg", "width": 3000, "height": 3000}

    client, _ = _studio(tmp_path, collector=collector)
    client.put("/api/artwork/override", json={"album": "A/B", "url": "http://x"})
    client.put("/api/artwork/override", json={"album": "A/B"})
    assert seen == [{"url": "http://x"}, None]


def test_a_dead_url_reports_an_error_but_keeps_the_override(tmp_path):
    """So the user can edit the URL rather than retype it."""
    def collector(album_path, out_dir, library_root, overrides):
        return {"status": "missing"}

    client, art = _studio(tmp_path, collector=collector)
    body = client.put("/api/artwork/override",
                      json={"album": "A/B", "url": "http://dead"}).get_json()
    assert body["error"]
    import json
    assert json.loads((art / "overrides.json").read_text())["A/B"] == {
        "url": "http://dead"}


def test_an_override_during_a_running_collection_does_not_write_the_manifest(tmp_path):
    from nfc_jukebox import web as web_module

    def collector(*a, **k):                       # pragma: no cover - not called
        raise AssertionError("must not collect while a run is in flight")

    client, art = _studio(tmp_path, collector=collector)
    # Simulate a bulk run in flight.
    client.application.extensions["collect_state"]["running"] = True
    body = client.put("/api/artwork/override",
                      json={"album": "A/B", "url": "http://x"}).get_json()
    assert body["collected"] is False
    assert not (art / "manifest.json").exists()


# --- albums that no longer exist -------------------------------------------
#
# The manifest keeps a row per album for ever, so deleting a folder left a
# ghost tile in the grid. Pruning has a sharp edge: an unmounted library looks
# exactly like every album having been deleted at once, so it is gated on the
# same mount check the status banner uses.


def test_a_deleted_album_is_dropped_from_the_grid(tmp_path):
    client, art = _studio(tmp_path, manifest={
        "Miles Davis/Kind of Blue": {"status": "ok", "file": "kob.jpg",
                                     "width": 3000, "height": 3000},
        "Gone/Away": {"status": "ok", "file": "gone.jpg",
                      "width": 3000, "height": 3000},
    })
    (art / "kob.jpg").write_bytes(b"\xff\xd8k")
    (art / "gone.jpg").write_bytes(b"\xff\xd8g")

    albums = client.get("/api/artwork/manifest").get_json()["albums"]

    assert [a["album"] for a in albums] == ["Miles Davis/Kind of Blue"]
    # Pruned for good, not merely hidden...
    assert "Gone/Away" not in json.loads((art / "manifest.json").read_text())
    # ...and its image is not left behind on the card.
    assert not (art / "gone.jpg").exists()
    assert (art / "kob.jpg").exists()


def test_pruning_keeps_the_hand_made_override(tmp_path):
    """A pin is a human judgement; the folder may come back renamed."""
    client, art = _studio(tmp_path, manifest={
        "Gone/Away": {"status": "ok", "file": "g.jpg", "width": 3000,
                      "height": 3000}})
    (art / "overrides.json").write_text(json.dumps(
        {"Gone/Away": {"url": "http://pinned"}}))

    client.get("/api/artwork/manifest")

    assert json.loads((art / "overrides.json").read_text()) == {
        "Gone/Away": {"url": "http://pinned"}}


def test_an_unmounted_library_prunes_nothing(tmp_path):
    """Every album looks deleted at once. Touch nothing."""
    empty = tmp_path / "empty"
    empty.mkdir()
    art = tmp_path / "art"
    art.mkdir()
    (art / "manifest.json").write_text(json.dumps(
        {"Miles Davis/Kind of Blue": {"status": "ok", "file": "k.jpg",
                                      "width": 3000, "height": 3000}}))
    (art / "k.jpg").write_bytes(b"\xff\xd8k")
    config = Config(library_root=empty, cards_file=tmp_path / "cards.yaml",
                    artwork_dir=art)
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store, collector=_offline_collector)
    app.config.update(TESTING=True)

    albums = app.test_client().get("/api/artwork/manifest").get_json()["albums"]

    assert [a["album"] for a in albums] == ["Miles Davis/Kind of Blue"]
    assert (art / "k.jpg").exists()


# --- cards pointing at albums that are gone --------------------------------


def test_a_card_for_a_missing_album_is_flagged_as_an_orphan(app_ctx):
    client, _, store = app_ctx
    _register(client, "aa", "Miles Davis/Kind of Blue")
    # Registration refuses an album that does not exist, so an orphan can only
    # be made the way it happens in life: register, then delete the folder.
    cards = store.load()
    cards["bb"] = Card(uid="bb", name="Swedish Metal Aid",
                       path="The State of Samuel/Swedish Metal Aid")
    store.save(cards)
    flagged = {c["uid"]: c["orphan"]
               for c in client.get("/api/cards").get_json()["cards"]}
    assert flagged == {"aa": False, "bb": True}


def test_no_card_is_an_orphan_when_the_library_is_not_mounted(tmp_path):
    """Otherwise a dropped NAS mount flags the whole registry as broken."""
    empty = tmp_path / "empty"
    empty.mkdir()
    config = Config(library_root=empty, cards_file=tmp_path / "cards.yaml")
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store)
    app.config.update(TESTING=True)
    store.save({"aa": Card(uid="aa", name="Kind of Blue",
                           path="Miles Davis/Kind of Blue")})
    cards = app.test_client().get("/api/cards").get_json()["cards"]
    assert [c["orphan"] for c in cards] == [False]


def test_pruning_is_skipped_while_a_collection_is_running(tmp_path):
    """The collector saves after every album; a read-modify-write here would
    drop whatever it wrote in between."""
    client, art = _studio(tmp_path, manifest={
        "Gone/Away": {"status": "ok", "file": "g.jpg", "width": 3000,
                      "height": 3000}})
    client.application.extensions["collect_state"]["running"] = True

    client.get("/api/artwork/manifest")

    assert "Gone/Away" in json.loads((art / "manifest.json").read_text())


# --- rescanning the library ------------------------------------------------
#
# /srv/music is a read-only CIFS mount, so OwnTone gets no inotify events and
# never notices a new album. The nightly cron only helps if the box happens to
# be powered on at 04:30, which it often is not.


class FakeLibrary:
    def __init__(self, updating=False):
        self.updates = 0
        self._updating = updating
        self.fail = None

    def update_library(self):
        if self.fail:
            raise self.fail
        self.updates += 1

    def library_status(self):
        return {"songs": 2085, "albums": 168, "updating": self._updating}

    def outputs(self):
        return [{"name": "Computer", "selected": True}]


def _library_app(app_ctx, fake):
    from nfc_jukebox import web as web_module
    _, controller, store = app_ctx
    app = web_module.create_app(Config(), controller, store, owntone=fake)
    app.config.update(TESTING=True)
    return app.test_client()


def test_rescan_asks_owntone_to_update(app_ctx):
    fake = FakeLibrary()
    client = _library_app(app_ctx, fake)
    body = client.post("/api/library/rescan").get_json()
    assert fake.updates == 1
    assert body["started"] is True


def test_rescan_reports_owntone_being_unreachable(app_ctx):
    fake = FakeLibrary()
    fake.fail = RuntimeError("connection refused")
    client = _library_app(app_ctx, fake)
    response = client.post("/api/library/rescan")
    assert response.status_code == 502
    assert response.get_json()["error"]


def test_status_reports_the_library_counts(app_ctx):
    client = _library_app(app_ctx, FakeLibrary(updating=True))
    body = client.get("/api/status").get_json()
    assert body["library"]["albums"] == 168
    assert body["library"]["updating"] is True


def test_status_survives_owntone_having_no_library_answer(app_ctx):
    """The status line must never be the thing that breaks."""
    client, _, _ = app_ctx          # built with no owntone at all
    assert client.get("/api/status").get_json()["library"] is None


def test_the_status_line_does_not_ask_owntone_every_second(app_ctx):
    """A rescan makes /api/library slow -- 13s was measured on the box -- and
    the page polls status once a second. Cache it like the outputs."""
    fake = FakeLibrary()
    calls = []
    original = fake.library_status
    fake.library_status = lambda: (calls.append(1), original())[1]
    client = _library_app(app_ctx, fake)

    for _ in range(5):
        client.get("/api/status")

    assert len(calls) == 1, f"asked OwnTone {len(calls)} times"


def test_a_slow_library_answer_leaves_the_last_one_on_screen(app_ctx, monkeypatch):
    fake = FakeLibrary()
    client = _library_app(app_ctx, fake)
    assert client.get("/api/status").get_json()["library"]["albums"] == 168

    def boom():
        raise RuntimeError("scanning, too busy")

    fake.library_status = boom
    import nfc_jukebox.web as web_module
    # Expire the cache without waiting for it. Capture the real clock first:
    # patching the name it is read through would make the lambda call itself.
    real_monotonic = time.monotonic
    monkeypatch.setattr(web_module.time, "monotonic",
                        lambda: real_monotonic() + 3600)
    assert client.get("/api/status").get_json()["library"]["albums"] == 168


# --- the page's JavaScript ---------------------------------------------------
#
# Nothing in this suite executes the template, so a syntax error in it ships
# silently and takes the whole page with it -- every handler, including the tab
# buttons, because one bad token stops the entire script parsing. That has
# happened twice: a chained Node.append().lastChild, and a `const` declared
# twice in one scope. Both were one `node --check` away from being caught.


def _page_script():
    """The contents of the template's <script> block."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src/nfc_jukebox/templates/index.html").read_text()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no <script> block in the template"
    return "\n".join(blocks)


def test_the_page_javascript_parses():
    """Syntax only -- it cannot catch a wrong id, but it catches the class of
    error that silently disables the entire page."""
    import os
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; cannot syntax-check the page script")

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(_page_script())
        path = handle.name
    result = subprocess.run([node, "--check", path],
                            capture_output=True, text=True)
    os.unlink(path)
    assert result.returncode == 0, result.stderr


def test_every_element_the_script_looks_up_exists_in_the_markup():
    """getElementById('typo') returns null and usually throws at the first
    property access, which is the other way this page breaks."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src/nfc_jukebox/templates/index.html").read_text()
    wanted = set(re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", html))
    present = set(re.findall(r"""\bid=["']([\w-]+)["']""", html))
    missing = sorted(wanted - present)
    assert not missing, f"script looks up ids that do not exist: {missing}"


# --- albums not collected yet ----------------------------------------------
#
# The grid was built from the manifest alone, so an album nobody had collected
# simply did not exist on the page. On a fresh box that meant an empty tab, and
# during a run the albums appeared one at a time -- so there was no way to see
# the size of the job or what was being worked on.


def test_an_album_with_no_manifest_entry_still_appears(tmp_path):
    client, _ = _studio(tmp_path)          # library has Miles Davis/Kind of Blue
    albums = client.get("/api/artwork/manifest").get_json()["albums"]
    assert [a["album"] for a in albums] == ["Miles Davis/Kind of Blue"]
    row = albums[0]
    assert row["verdict"] == "pending"
    assert row["file"] is None


def test_a_pending_album_is_not_reported_as_a_failure(tmp_path):
    """'bad' means looked for and not found; these have not been looked for."""
    client, _ = _studio(tmp_path)
    row = client.get("/api/artwork/manifest").get_json()["albums"][0]
    assert row["verdict"] != "bad"
    assert row["why"] == "not collected yet"


def test_collected_and_pending_albums_appear_together(tmp_path):
    (tmp_path / "Fleetwood Mac" / "Rumours").mkdir(parents=True)
    client, art = _studio(tmp_path, manifest={
        "Miles Davis/Kind of Blue": {"status": "ok", "file": "k.jpg",
                                     "width": 3000, "height": 3000}})
    albums = {a["album"]: a["verdict"]
              for a in client.get("/api/artwork/manifest").get_json()["albums"]}
    assert albums["Miles Davis/Kind of Blue"] == "fetched"
    assert albums["Fleetwood Mac/Rumours"] == "pending"


def test_an_album_with_a_card_is_not_listed(tmp_path):
    """The studio is the print queue: albums awaiting a card, nothing else."""
    client, _ = _studio(tmp_path)
    client.post("/api/cards", json={"uid": "aa",
                                    "path": "Miles Davis/Kind of Blue"})
    albums = client.get("/api/artwork/manifest").get_json()["albums"]
    assert albums == []


def test_an_unreadable_library_falls_back_to_the_manifest(tmp_path):
    """Listing nothing must not blank a grid that has real results in it."""
    empty = tmp_path / "empty"
    empty.mkdir()
    art = tmp_path / "art"
    art.mkdir()
    (art / "manifest.json").write_text(json.dumps(
        {"Miles Davis/Kind of Blue": {"status": "ok", "file": "k.jpg",
                                      "width": 3000, "height": 3000}}))
    config = Config(library_root=empty, cards_file=tmp_path / "cards.yaml",
                    artwork_dir=art)
    store = CardStore(config.cards_file)
    controller = Controller(FakeOwnTone(), store, FakeSnapshot(), config,
                            clock=FakeClock())
    app = create_app(config, controller, store, collector=_offline_collector)
    app.config.update(TESTING=True)
    albums = app.test_client().get("/api/artwork/manifest").get_json()["albums"]
    assert [a["album"] for a in albums] == ["Miles Davis/Kind of Blue"]


def test_the_admin_page_is_never_cached(app_ctx):
    """The page is the app. A browser holding yesterday's copy runs yesterday's
    JavaScript against today's API, which looks like the box misbehaving --
    and there is no version in the URL to break the cache with, because the
    page is served from '/'."""
    client, _, _ = app_ctx
    cache = client.get("/").headers.get("Cache-Control", "")
    assert "no-store" in cache


def test_the_library_listing_is_not_walked_on_every_request(app_ctx, monkeypatch):
    """_album_rows globs the library over SMB. /api/cards and /api/albums are
    polled once a second by the page, and 185 albums over CIFS measured 7.3s
    on the box -- so the walk has to be cached, not repeated."""
    import nfc_jukebox.web as web_module
    client, _, _ = app_ctx

    walks = []
    real_glob = web_module.Path.glob

    def counting_glob(self, pattern):
        walks.append(pattern)
        return real_glob(self, pattern)

    monkeypatch.setattr(web_module.Path, "glob", counting_glob)
    for _ in range(5):
        client.get("/api/cards")
    assert len(walks) <= 1, f"walked the library {len(walks)} times"


# --- reprinting albums that already have cards ------------------------------
#
# The studio is normally the print queue: albums awaiting a card. Replacing a
# whole set of cards needs the opposite view, and artwork for the 131 albums
# that have never been collected because they were already done.


def test_assigned_albums_are_hidden_by_default(tmp_path):
    client, _ = _studio(tmp_path)
    client.post("/api/cards", json={"uid": "aa",
                                    "path": "Miles Davis/Kind of Blue"})
    assert client.get("/api/artwork/manifest").get_json()["albums"] == []


def test_all_shows_albums_that_already_have_cards(tmp_path):
    client, _ = _studio(tmp_path)
    client.post("/api/cards", json={"uid": "aa",
                                    "path": "Miles Davis/Kind of Blue"})
    albums = client.get("/api/artwork/manifest?all=1").get_json()["albums"]
    assert [a["album"] for a in albums] == ["Miles Davis/Kind of Blue"]
    assert albums[0]["assigned"] is True


def test_collecting_all_targets_albums_that_have_cards(tmp_path):
    seen = []

    def collector(album_path, out_dir, library_root, overrides):
        seen.append(album_path)
        return {"status": "ok", "file": "x.jpg", "width": 3000, "height": 3000}

    client, _ = _studio(tmp_path, collector=collector)
    client.post("/api/cards", json={"uid": "aa",
                                    "path": "Miles Davis/Kind of Blue"})

    assert client.post("/api/artwork/collect").get_json()["total"] == 0
    body = client.post("/api/artwork/collect", json={"all": True}).get_json()
    assert body["total"] == 1


def test_sheets_only_print_the_albums_on_screen(tmp_path):
    """What prints must match what the grid shows, or an untick produces a
    surprise forty-page PDF built from whatever was collected earlier."""
    (tmp_path / "Fleetwood Mac" / "Rumours").mkdir(parents=True)
    client, art = _studio(tmp_path, manifest={
        "Miles Davis/Kind of Blue": {"status": "ok", "file": "a.jpg",
                                     "width": 3000, "height": 3000},
        "Fleetwood Mac/Rumours": {"status": "ok", "file": "b.jpg",
                                  "width": 3000, "height": 3000}})
    client.post("/api/cards", json={"uid": "aa",
                                    "path": "Miles Davis/Kind of Blue"})

    from nfc_jukebox.cardart import build_sheets
    import nfc_jukebox.web as web_module
    calls = {}

    def fake_sheets(directory, out_path, min_px, include_suspect, only=None):
        calls["only"] = only
        return {"cards": 1, "pages": 1, "skipped": []}

    web_module.__dict__.setdefault("_", None)
    import nfc_jukebox.cardart as cardart
    original = cardart.build_sheets
    cardart.build_sheets = fake_sheets
    try:
        client.post("/api/artwork/sheets", json={})
        assert calls["only"] == {"Fleetwood Mac/Rumours"}
        client.post("/api/artwork/sheets", json={"all": True})
        assert calls["only"] is None
    finally:
        cardart.build_sheets = original


# --- scans posted from a desk reader ----------------------------------------


def test_a_posted_scan_shows_up_as_the_last_seen_card(app_ctx):
    client, controller, _ = app_ctx
    body = client.post("/api/scan", json={"uid": "AB:CD:EF:01"}).get_json()
    assert body["uid"] == "abcdef01"
    assert controller.last_seen_uid == "abcdef01"
    assert client.get("/api/status").get_json()["last_seen_uid"] == "abcdef01"


def test_a_posted_scan_says_whether_the_card_is_known(app_ctx):
    client, _, store = app_ctx
    store.save({"aa": Card(uid="aa", name="Kind of Blue",
                           path="Miles Davis/Kind of Blue")})
    assert client.post("/api/scan", json={"uid": "aa"}).get_json()["known"] is True
    assert client.post("/api/scan", json={"uid": "bb"}).get_json()["known"] is False


def test_a_scan_without_a_uid_is_a_400(app_ctx):
    client, _, _ = app_ctx
    assert client.post("/api/scan", json={}).status_code == 400


def test_a_posted_scan_does_not_touch_playback(app_ctx):
    """A tap at a desk must never interrupt the record in the room."""
    client, controller, _ = app_ctx
    controller.state = State.PLAYING
    controller.now_playing = "Rumours"
    client.post("/api/scan", json={"uid": "aa"})
    assert controller.state is State.PLAYING
    assert controller.now_playing == "Rumours"
