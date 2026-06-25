import textwrap
from pathlib import Path

import pytest

from manabot.buylist import BuyListError, load_buylist, append_to_buylist, remove_from_buylist, remove_purchases_fifo, edit_buylist_entry
from manabot.models import BuyListItem, Condition, Finish


FIXTURE = Path(__file__).parent / "fixtures" / "sample_buylist.csv"


def test_load_valid_fixture():
    items = load_buylist(FIXTURE)
    assert len(items) == 4


def test_scryfall_id_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].scryfall_id == "e3285e6b-3e79-4d7c-bf96-d920f973b122"
    assert items[1].scryfall_id == "2fab0ea3-7664-4ee8-b520-7f3a4e966aae"
    assert items[2].scryfall_id is None  # Black Lotus has no scryfall_id in fixture


def test_condition_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].min_condition == Condition.LP
    assert items[1].min_condition == Condition.MP
    assert items[2].min_condition == Condition.NM


def test_foil_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].foil == Finish.NONFOIL
    assert items[1].foil == Finish.ANY
    assert items[3].foil == Finish.FOIL


def test_allowed_sets_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].allowed_sets == []
    assert items[2].allowed_sets == ["LEA"]


def test_in_universe_only_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].in_universe_only is False
    assert items[2].in_universe_only is True  # Black Lotus


def test_tags_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].tags == ["burn-deck"]
    assert items[1].tags == ["legacy", "customer:John"]


def test_extra_columns_ignored():
    # The fixture has a 'notes' column — should not raise
    items = load_buylist(FIXTURE)
    assert len(items) == 4


def test_missing_required_column(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text("card_name,target_quantity,max_price_usd\nLightning Bolt,4,1.50\n", encoding="utf-8")
    with pytest.raises(BuyListError, match="min_condition"):
        load_buylist(csv)


def test_non_numeric_price(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,4,expensive,NM\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="max_price_usd"):
        load_buylist(csv)


def test_invalid_condition(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,4,1.50,MINT\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="min_condition"):
        load_buylist(csv)


def test_negative_quantity(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,-1,1.50,NM\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="target_quantity"):
        load_buylist(csv)


def test_excel_bom_encoding(tmp_path):
    csv = tmp_path / "bom.csv"
    csv.write_bytes(
        b"\xef\xbb\xbfcard_name,target_quantity,max_price_usd,min_condition\r\nLightning Bolt,4,1.50,NM\r\n"
    )
    items = load_buylist(csv)
    assert items[0].card_name == "Lightning Bolt"


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_buylist(Path("nonexistent.csv"))


# ── append_to_buylist ─────────────────────────────────────────────────────────

def _make_item(**kwargs) -> BuyListItem:
    defaults = dict(
        card_name="Lightning Bolt",
        target_quantity=4,
        max_price_usd=1.50,
        min_condition=Condition.NM,
        foil=Finish.ANY,
        allowed_sets=[],
        tags=[],
    )
    defaults.update(kwargs)
    return BuyListItem(**defaults)


def test_append_creates_file(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item())
    assert path.exists()
    items = load_buylist(path)
    assert len(items) == 1
    assert items[0].card_name == "Lightning Bolt"


def test_append_preserves_existing_rows(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    append_to_buylist(path, _make_item(card_name="Dark Ritual", max_price_usd=0.75))
    items = load_buylist(path)
    assert len(items) == 2
    assert items[1].card_name == "Dark Ritual"


def test_append_tags_stored(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(tags=["user:Alice", "burn-deck"]))
    items = load_buylist(path)
    assert "user:Alice" in items[0].tags
    assert "burn-deck" in items[0].tags


def test_append_optional_fields(tmp_path):
    path = tmp_path / "bl.csv"
    item = _make_item(
        foil=Finish.FOIL,
        allowed_sets=["LEA", "LEB"],
        min_condition=Condition.LP,
        scryfall_id="abc123",
    )
    append_to_buylist(path, item)
    items = load_buylist(path)
    assert items[0].foil == Finish.FOIL
    assert items[0].allowed_sets == ["LEA", "LEB"]
    assert items[0].min_condition == Condition.LP
    assert items[0].scryfall_id == "abc123"


def test_append_to_existing_fixture(tmp_path):
    import shutil
    dest = tmp_path / "bl.csv"
    shutil.copy(FIXTURE, dest)
    original_count = len(load_buylist(dest))
    append_to_buylist(dest, _make_item(card_name="Force of Will"))
    items = load_buylist(dest)
    assert len(items) == original_count + 1
    assert items[-1].card_name == "Force of Will"


# ── remove_from_buylist ───────────────────────────────────────────────────────

def test_remove_single_card(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    append_to_buylist(path, _make_item(card_name="Dark Ritual"))
    removed = remove_from_buylist(path, ["Lightning Bolt"])
    assert removed == 1
    items = load_buylist(path)
    assert len(items) == 1
    assert items[0].card_name == "Dark Ritual"


def test_remove_case_insensitive(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    removed = remove_from_buylist(path, ["lightning bolt"])
    assert removed == 1
    assert load_buylist(path) == []


def test_remove_multiple_cards(tmp_path):
    path = tmp_path / "bl.csv"
    for name in ["Lightning Bolt", "Dark Ritual", "Counterspell"]:
        append_to_buylist(path, _make_item(card_name=name))
    removed = remove_from_buylist(path, ["Lightning Bolt", "Counterspell"])
    assert removed == 2
    items = load_buylist(path)
    assert len(items) == 1
    assert items[0].card_name == "Dark Ritual"


def test_remove_nonexistent_returns_zero(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    removed = remove_from_buylist(path, ["Force of Will"])
    assert removed == 0
    assert len(load_buylist(path)) == 1


def test_remove_on_missing_file_returns_zero(tmp_path):
    assert remove_from_buylist(tmp_path / "none.csv", ["Lightning Bolt"]) == 0


# ── remove_purchases_fifo ─────────────────────────────────────────────────────

def test_fifo_removes_all_when_qty_minus_one(tmp_path):
    path = tmp_path / "bl.csv"
    for _ in range(3):
        append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", -1)])
    assert len(affected) == 3
    assert load_buylist(path) == []


def test_fifo_exact_quantity_removes_one_row(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4))
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=2))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", 4)])
    # First row (qty 4) fully consumed; second row untouched
    assert len(affected) == 1
    assert affected[0]["qty_purchased"] == "4"
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].target_quantity == 2


def test_fifo_partial_quantity_decrements_row(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", 2)])
    assert len(affected) == 1
    assert affected[0]["qty_purchased"] == "2"
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].target_quantity == 2


def test_fifo_spans_multiple_rows(tmp_path):
    path = tmp_path / "bl.csv"
    # Three rows with qty 2 each, purchase 5 → remove first two rows, decrement third to 1
    for _ in range(3):
        append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=2))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", 5)])
    assert len(affected) == 3
    assert affected[0]["qty_purchased"] == "2"
    assert affected[1]["qty_purchased"] == "2"
    assert affected[2]["qty_purchased"] == "1"
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].target_quantity == 1


def test_fifo_does_not_touch_other_cards(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4))
    append_to_buylist(path, _make_item(card_name="Dark Ritual", target_quantity=2))
    remove_purchases_fifo(path, [("Lightning Bolt", 4)])
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].card_name == "Dark Ritual"


def test_fifo_returns_tags_in_snapshot(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["user:Alice", "uid:111"]))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", -1)])
    assert any("uid:111" in (r.get("tags") or "") for r in affected)


def test_fifo_no_match_returns_empty(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    affected = remove_purchases_fifo(path, [("Dark Ritual", 1)])
    assert affected == []
    assert len(load_buylist(path)) == 1


def test_fifo_missing_file_returns_empty(tmp_path):
    assert remove_purchases_fifo(tmp_path / "none.csv", [("Lightning Bolt", 1)]) == []


def test_fifo_uid_filter_only_removes_matching_user(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["user:Alice", "uid:111"]))
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["user:Bob", "uid:222"]))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", -1)], uid="111")
    assert len(affected) == 1
    assert "uid:111" in affected[0].get("tags", "")
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert "Bob" in remaining[0].tags[0]


def test_fifo_uid_filter_no_match_leaves_file_unchanged(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["user:Bob", "uid:222"]))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", -1)], uid="999")
    assert affected == []
    assert len(load_buylist(path)) == 1


def test_fifo_uid_filter_none_removes_all(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["uid:111"]))
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["uid:222"]))
    affected = remove_purchases_fifo(path, [("Lightning Bolt", -1)], uid=None)
    assert len(affected) == 2


# ── edit_buylist_entry ────────────────────────────────────────────────────────

def test_edit_updates_quantity(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4))
    original = edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "2"})
    assert original is not None
    assert original["target_quantity"] == "4"
    items = load_buylist(path)
    assert items[0].target_quantity == 2


def test_edit_updates_price(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", max_price_usd=1.50))
    edit_buylist_entry(path, "Lightning Bolt", {"max_price_usd": "2.00"})
    assert load_buylist(path)[0].max_price_usd == 2.00


def test_edit_updates_condition(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", min_condition=Condition.NM))
    edit_buylist_entry(path, "Lightning Bolt", {"min_condition": "LP"})
    assert load_buylist(path)[0].min_condition == Condition.LP


def test_edit_clears_allowed_sets(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", allowed_sets=["LEA"]))
    edit_buylist_entry(path, "Lightning Bolt", {"allowed_sets": ""})
    assert load_buylist(path)[0].allowed_sets == []


def test_edit_none_values_not_applied(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4, max_price_usd=1.50))
    edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "2", "max_price_usd": None})
    item = load_buylist(path)[0]
    assert item.target_quantity == 2
    assert item.max_price_usd == 1.50  # unchanged


def test_edit_returns_original_before_change(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4))
    original = edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "1"})
    assert original is not None
    assert original["target_quantity"] == "4"


def test_edit_case_insensitive_name(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    result = edit_buylist_entry(path, "lightning bolt", {"target_quantity": "1"})
    assert result is not None


def test_edit_uid_filter_matches_owner(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["uid:111"]))
    result = edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "1"}, uid="111")
    assert result is not None


def test_edit_uid_filter_skips_other_user(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", tags=["uid:111"]))
    result = edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "1"}, uid="999")
    assert result is None
    assert load_buylist(path)[0].target_quantity == 4  # unchanged


def test_edit_edits_first_row_fifo(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=4, max_price_usd=1.00))
    append_to_buylist(path, _make_item(card_name="Lightning Bolt", target_quantity=2, max_price_usd=2.00))
    edit_buylist_entry(path, "Lightning Bolt", {"target_quantity": "1"})
    items = load_buylist(path)
    assert items[0].target_quantity == 1   # first row edited
    assert items[1].target_quantity == 2   # second row untouched


def test_edit_no_match_returns_none(tmp_path):
    path = tmp_path / "bl.csv"
    append_to_buylist(path, _make_item(card_name="Lightning Bolt"))
    assert edit_buylist_entry(path, "Dark Ritual", {"target_quantity": "1"}) is None


def test_edit_missing_file_returns_none(tmp_path):
    assert edit_buylist_entry(tmp_path / "none.csv", "Lightning Bolt", {"target_quantity": "1"}) is None
