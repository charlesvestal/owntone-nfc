import logging

from nfc_jukebox.cards import Card, CardStore, name_for_path, normalise_uid


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
        "badbad01: just a string\n"       # not a mapping at all
        "badbad02:\n"                     # null entry
    )
    store = CardStore(path)
    with caplog.at_level(logging.WARNING):
        loaded = store.load()
    assert set(loaded) == {"04a2b3c4"}
    assert loaded["04a2b3c4"].name == "Kind of Blue"
    logged = caplog.text
    for uid in ("deadbeef", "badbad01", "badbad02"):
        assert uid in logged


def test_entry_without_a_name_is_valid_and_takes_the_album_folder(tmp_path):
    # The path already names the album, so `name` is optional - a card entry
    # missing one must load, not be discarded as malformed.
    path = tmp_path / "cards.yaml"
    path.write_text("cafebabe:\n  path: Miles Davis/Kind of Blue\n")
    loaded = CardStore(path).load()
    assert loaded["cafebabe"].name == "Kind of Blue"


def test_name_for_path_uses_the_last_segment():
    assert name_for_path("Miles Davis/Kind of Blue") == "Kind of Blue"
    assert name_for_path("Miles Davis/Kind of Blue/") == "Kind of Blue"
    assert name_for_path("Single") == "Single"


def test_corrupt_file_degrades_to_empty_instead_of_raising(tmp_path, caplog):
    path = tmp_path / "cards.yaml"
    path.write_text("04a2b3c4:\n  name: [unclosed\n")
    store = CardStore(path)
    with caplog.at_level(logging.ERROR):
        assert store.load() == {}
    assert "cards.yaml" in caplog.text


def test_corrupt_file_is_preserved_aside_not_overwritten(tmp_path):
    path = tmp_path / "cards.yaml"
    original = "04a2b3c4:\n  name: [unclosed\n"
    path.write_text(original)
    store = CardStore(path)
    store.load()
    salvaged = list(tmp_path.glob("cards.yaml.corrupt*"))
    assert len(salvaged) == 1
    assert salvaged[0].read_text() == original
    # The bad file is out of the way, so the next tap does not re-explode.
    assert not path.exists()
    assert store.load() == {}


def test_truncated_zero_length_file_loads_as_empty(tmp_path):
    path = tmp_path / "cards.yaml"
    path.write_text("")
    assert CardStore(path).load() == {}
    # Empty is not corrupt: nothing to preserve.
    assert list(tmp_path.glob("cards.yaml.corrupt*")) == []


def test_top_level_scalar_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "cards.yaml"
    path.write_text("just a string\n")
    store = CardStore(path)
    assert store.load() == {}
    assert len(list(tmp_path.glob("cards.yaml.corrupt*"))) == 1


def test_unreadable_file_degrades_to_empty(tmp_path, monkeypatch):
    path = tmp_path / "cards.yaml"
    path.write_text("04a2b3c4:\n  name: X\n  path: A/B\n")
    store = CardStore(path)

    def boom(*args, **kwargs):
        raise PermissionError(13, "nope")

    monkeypatch.setattr(CardStore, "_read", staticmethod(boom))
    assert store.load() == {}
    # An OS-level read failure must not destroy the registry.
    assert path.exists()


def test_get_on_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "cards.yaml"
    path.write_text("04a2b3c4:\n  name: [unclosed\n")
    assert CardStore(path).get("04a2b3c4") is None


def test_save_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "cards.yaml"
    store = CardStore(path)
    store.save({"04a2b3c4": Card(uid="04a2b3c4", name="X", path="A/B")})
    assert [p.name for p in tmp_path.iterdir()] == ["cards.yaml"]


# --- duplicate detection ---------------------------------------------------
#
# Two cards on one album is always a mistake here: every album gets exactly one
# card. With ~130 registered it is invisible until two cards turn out to play
# the same record, so the admin page flags it.


def _card(uid, path, name=None):
    from nfc_jukebox.cards import Card
    return Card(uid=uid, name=name or path.split("/")[-1], path=path)


def _store(*cards):
    return {c.uid: c for c in cards}


def test_no_cards_have_no_duplicates():
    from nfc_jukebox.cards import duplicate_paths
    assert duplicate_paths({}) == {}


def test_distinct_albums_are_not_duplicates():
    from nfc_jukebox.cards import duplicate_paths
    cards = _store(_card("aa", "Pavement/Wowee Zowee"),
                   _card("bb", "Mogwai/Young Team"))
    assert duplicate_paths(cards) == {}


def test_two_cards_on_one_album_are_reported_with_both_uids():
    from nfc_jukebox.cards import duplicate_paths
    cards = _store(_card("bb", "Pavement/Wowee Zowee"),
                   _card("aa", "Pavement/Wowee Zowee"),
                   _card("cc", "Mogwai/Young Team"))
    # UIDs sorted, so the report is stable between calls.
    assert duplicate_paths(cards) == {"Pavement/Wowee Zowee": ["aa", "bb"]}


def test_three_cards_on_one_album_are_all_reported():
    from nfc_jukebox.cards import duplicate_paths
    cards = _store(_card("aa", "Pavement/Wowee Zowee"),
                   _card("bb", "Pavement/Wowee Zowee"),
                   _card("cc", "Pavement/Wowee Zowee"))
    assert duplicate_paths(cards) == {"Pavement/Wowee Zowee": ["aa", "bb", "cc"]}


def test_two_separate_pairs_are_both_reported():
    from nfc_jukebox.cards import duplicate_paths
    cards = _store(_card("aa", "Pavement/Wowee Zowee"),
                   _card("bb", "Pavement/Wowee Zowee"),
                   _card("cc", "Mogwai/Young Team"),
                   _card("dd", "Mogwai/Young Team"))
    assert duplicate_paths(cards) == {
        "Pavement/Wowee Zowee": ["aa", "bb"],
        "Mogwai/Young Team": ["cc", "dd"],
    }


def test_the_album_path_is_the_identity_not_the_label():
    """Two cards labelled differently still point at one record."""
    from nfc_jukebox.cards import duplicate_paths
    cards = _store(_card("aa", "Pavement/Wowee Zowee", name="Wowee Zowee"),
                   _card("bb", "Pavement/Wowee Zowee", name="for the car"))
    assert duplicate_paths(cards) == {"Pavement/Wowee Zowee": ["aa", "bb"]}
