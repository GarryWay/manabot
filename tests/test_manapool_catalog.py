from manabot.api.manapool_catalog import build_name_index, build_number_index


def test_build_number_index_keys_by_set_and_number():
    records = [
        {"set_code": "teoe", "number": "4", "name": "Lander", "scryfall_id": "lander-4"},
        {"set_code": "teoe", "number": "7", "name": "Lander", "scryfall_id": "lander-7"},
    ]
    index = build_number_index(records)
    assert index[("TEOE", "4")]["scryfall_id"] == "lander-4"
    assert index[("TEOE", "7")]["scryfall_id"] == "lander-7"


def test_build_number_index_uppercases_set_code():
    records = [{"set_code": "teoe", "number": "4", "scryfall_id": "lander-4"}]
    index = build_number_index(records)
    assert ("TEOE", "4") in index
    assert ("teoe", "4") not in index


def test_build_number_index_skips_records_with_no_number():
    records = [
        {"set_code": "TEOE", "number": "", "scryfall_id": "no-number"},
        {"set_code": "TEOE", "scryfall_id": "missing-number-key"},  # no "number" key at all
        {"set_code": "TEOE", "number": "4", "scryfall_id": "has-number"},
    ]
    index = build_number_index(records)
    assert len(index) == 1
    assert index[("TEOE", "4")]["scryfall_id"] == "has-number"


def test_build_number_index_does_not_collapse_same_name_different_numbers():
    """The whole point: unlike build_name_index, every distinct number gets its own
    entry -- reused names (e.g. multiple 'Lander' printings) don't collide here."""
    records = [
        {"set_code": "TEOE", "number": "4", "name": "Lander", "scryfall_id": "lander-4", "price_market": 40},
        {"set_code": "TEOE", "number": "5", "name": "Lander", "scryfall_id": "lander-5", "price_market": 50},
        {"set_code": "TEOE", "number": "6", "name": "Lander", "scryfall_id": "lander-6", "price_market": 60},
    ]
    index = build_number_index(records)
    assert len(index) == 3
    assert {v["scryfall_id"] for v in index.values()} == {"lander-4", "lander-5", "lander-6"}


def test_build_name_index_still_collapses_same_name_to_highest_price():
    """Documents the existing, still-intentional behavior of build_name_index --
    contrast with build_number_index above, which is the correct tool when a name
    is known to be reused (see pricer.apply_double_sided_upgrades)."""
    records = [
        {"set_code": "TEOE", "number": "4", "name": "Lander", "scryfall_id": "lander-4", "price_market": 40},
        {"set_code": "TEOE", "number": "8", "name": "Lander", "scryfall_id": "lander-8", "price_market": 80},
    ]
    index = build_name_index(records)
    assert len(index) == 1
    assert index[("TEOE", "Lander")]["scryfall_id"] == "lander-8"
