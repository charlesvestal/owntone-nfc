import logging

from nfc_jukebox.cards import Card, CardStore, normalise_uid


def test_normalise_uid_strips_separators_and_lowercases():
    assert normalise_uid("04:A2:B3:C4") == "04a2b3c4"
    assert normalise_uid("04 a2 b3 c4") == "04a2b3c4"
    assert normalise_uid("04A2B3C4") == "04a2b3c4"


def test_load_missing_file_returns_empty(tmp_path):
    store = CardStore(tmp_path / "nope.yaml")
    assert store.load() == {}


def test_round_trip(tmp_path):
    path = tmp_path / "cards.yaml"
    store = CardStore(path)
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="Kind of Blue",
                                 path="Miles Davis/Kind of Blue")})
    loaded = store.load()
    assert loaded["04a2b3c4"].name == "Kind of Blue"
    assert loaded["04a2b3c4"].path == "Miles Davis/Kind of Blue"


def test_lookup_normalises_incoming_uid(tmp_path):
    path = tmp_path / "cards.yaml"
    store = CardStore(path)
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="X", path="A/B")})
    assert store.get("04:A2:B3:C4") is not None


def test_get_unknown_uid_returns_none(tmp_path):
    store = CardStore(tmp_path / "nope.yaml")
    assert store.get("deadbeef") is None


def test_malformed_entry_does_not_lose_the_whole_registry(tmp_path, caplog):
    path = tmp_path / "cards.yaml"
    path.write_text(
        "04a2b3c4:\n"
        "  name: Kind of Blue\n"
        "  path: Miles Davis/Kind of Blue\n"
        "deadbeef:\n"
        "  name: Missing Path\n"          # no path key
        "cafebabe:\n"
        "  path: Only/A/Path\n"           # no name key
        "badbad01: just a string\n"       # not a mapping at all
        "badbad02:\n"                     # null entry
    )
    store = CardStore(path)
    with caplog.at_level(logging.WARNING):
        loaded = store.load()
    assert set(loaded) == {"04a2b3c4"}
    assert loaded["04a2b3c4"].name == "Kind of Blue"
    logged = caplog.text
    for uid in ("deadbeef", "cafebabe", "badbad01", "badbad02"):
        assert uid in logged
