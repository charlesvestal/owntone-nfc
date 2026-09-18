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
