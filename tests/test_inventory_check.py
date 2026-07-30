from unittest.mock import MagicMock

from manabot.buylist import append_to_buylist, load_buylist
from manabot.inventory_check import apply_force_remove, find_overlap
from manabot.models import BuyListItem, Condition, Finish, SellerListing

BOLT_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"
COUNTERSPELL_ID = "5f8287b1-5b0e-4291-9789-c6bfb7f177a4"


def make_item(
    card_name="Lightning Bolt",
    scryfall_id=None,
    target_quantity=4,
    max_price=2.00,
    min_condition=Condition.LP,
) -> BuyListItem:
    return BuyListItem(
        card_name=card_name,
        target_quantity=target_quantity,
        max_price_usd=max_price,
        min_condition=min_condition,
        scryfall_id=scryfall_id,
    )


def make_seller_listing(
    scryfall_id=BOLT_ID,
    card_name="Lightning Bolt",
    set_code="LEB",
    condition=Condition.NM,
    finish=Finish.NONFOIL,
    quantity=2,
    price=3.00,
) -> SellerListing:
    return SellerListing(
        inventory_id="inv-1",
        product_id="prod-1",
        scryfall_id=scryfall_id,
        card_name=card_name,
        set_code=set_code,
        condition=condition,
        finish=finish,
        language="EN",
        quantity=quantity,
        price_usd=price,
    )


def test_no_overlap_when_inventory_empty():
    buy_list = [make_item()]
    assert find_overlap(buy_list, []) == []


def test_matches_by_scryfall_id():
    buy_list = [make_item(scryfall_id=BOLT_ID)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID)]
    overlaps = find_overlap(buy_list, inventory)
    assert len(overlaps) == 1
    assert overlaps[0].buy_list_item.card_name == "Lightning Bolt"
    assert overlaps[0].matches[0].scryfall_id == BOLT_ID


def test_matches_by_normalized_name_when_no_scryfall_id():
    buy_list = [make_item(card_name="lightning bolt!", scryfall_id=None)]
    inventory = [make_seller_listing(card_name="Lightning Bolt")]
    overlaps = find_overlap(buy_list, inventory)
    assert len(overlaps) == 1


def test_no_match_for_different_card():
    buy_list = [make_item(card_name="Counterspell", scryfall_id=COUNTERSPELL_ID)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID, card_name="Lightning Bolt")]
    assert find_overlap(buy_list, inventory) == []

    buy_list = [make_item(card_name="Counterspell", scryfall_id=None)]
    assert find_overlap(buy_list, inventory) == []


def test_scryfall_id_pinned_item_ignores_name_only_inventory_match():
    """A buy list item pinned to a scryfall_id should not match inventory that
    only shares a name (e.g. a different printing indexed under the same name)."""
    buy_list = [make_item(scryfall_id=COUNTERSPELL_ID)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID, card_name="Lightning Bolt")]
    assert find_overlap(buy_list, inventory) == []


def test_ignores_condition_and_finish_when_matching():
    """Any condition/finish already in stock counts — we just don't want to re-buy the card."""
    buy_list = [make_item(scryfall_id=BOLT_ID, min_condition=Condition.NM)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID, condition=Condition.HP, finish=Finish.FOIL)]
    overlaps = find_overlap(buy_list, inventory)
    assert len(overlaps) == 1


def test_total_quantity_sums_multiple_matching_listings():
    buy_list = [make_item(scryfall_id=BOLT_ID)]
    inventory = [
        make_seller_listing(scryfall_id=BOLT_ID, set_code="LEB", quantity=2),
        make_seller_listing(scryfall_id=BOLT_ID, set_code="M11", quantity=3),
    ]
    overlaps = find_overlap(buy_list, inventory)
    assert len(overlaps) == 1
    assert overlaps[0].total_quantity == 5
    assert len(overlaps[0].matches) == 2


def test_items_without_overlap_are_omitted():
    buy_list = [
        make_item(card_name="Lightning Bolt", scryfall_id=BOLT_ID),
        make_item(card_name="Counterspell", scryfall_id=COUNTERSPELL_ID),
    ]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID)]
    overlaps = find_overlap(buy_list, inventory)
    assert len(overlaps) == 1
    assert overlaps[0].buy_list_item.card_name == "Lightning Bolt"


# ── overlap_quantity ─────────────────────────────────────────────────────────

def test_overlap_quantity_capped_by_inventory_when_inventory_is_smaller():
    buy_list = [make_item(scryfall_id=BOLT_ID, target_quantity=10)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID, quantity=4)]
    overlaps = find_overlap(buy_list, inventory)
    assert overlaps[0].overlap_quantity == 4


def test_overlap_quantity_capped_by_target_when_target_is_smaller():
    buy_list = [make_item(scryfall_id=BOLT_ID, target_quantity=4)]
    inventory = [make_seller_listing(scryfall_id=BOLT_ID, quantity=10)]
    overlaps = find_overlap(buy_list, inventory)
    assert overlaps[0].overlap_quantity == 4


# ── apply_force_remove ───────────────────────────────────────────────────────

def _buylist_with(tmp_path, item: BuyListItem):
    path = tmp_path / "buylist.csv"
    append_to_buylist(path, item)
    return path


def test_force_remove_only_takes_what_the_buylist_needs_leaving_inventory_surplus(tmp_path):
    """Regression: force_remove used to delete every matching listing in full,
    even when inventory quantity far exceeded what the buy list wanted."""
    item = make_item(card_name="Lightning Bolt", scryfall_id=BOLT_ID, target_quantity=4)
    path = _buylist_with(tmp_path, item)
    surplus_listing = make_seller_listing(scryfall_id=BOLT_ID, quantity=10)
    overlaps = find_overlap([item], [surplus_listing])

    client = MagicMock()
    result = apply_force_remove(overlaps, client, path)

    # Only 4 of the 10 units should be pulled — via a quantity trim, not a delete.
    client.delete_seller_listing.assert_not_called()
    client.update_seller_listing_price.assert_called_once_with(surplus_listing, surplus_listing.price_usd, 6)
    assert result.inventory_qty_removed == 4
    assert result.listings_updated == 1
    assert result.listings_deleted == 0

    # Buy list entry fully consumed (wanted 4, got 4).
    assert load_buylist(path) == []


def test_force_remove_deletes_listing_when_fully_consumed(tmp_path):
    item = make_item(card_name="Lightning Bolt", scryfall_id=BOLT_ID, target_quantity=10)
    path = _buylist_with(tmp_path, item)
    listing = make_seller_listing(scryfall_id=BOLT_ID, quantity=4)
    overlaps = find_overlap([item], [listing])

    client = MagicMock()
    result = apply_force_remove(overlaps, client, path)

    client.delete_seller_listing.assert_called_once_with(listing)
    client.update_seller_listing_price.assert_not_called()
    assert result.inventory_qty_removed == 4
    assert result.listings_deleted == 1

    # Buy list decremented by only the 4 units that were actually available.
    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].target_quantity == 6


def test_force_remove_spans_multiple_listings_and_stops_once_satisfied(tmp_path):
    item = make_item(card_name="Lightning Bolt", scryfall_id=BOLT_ID, target_quantity=5)
    path = _buylist_with(tmp_path, item)
    listing_a = make_seller_listing(scryfall_id=BOLT_ID, set_code="LEB", quantity=3)
    listing_b = make_seller_listing(scryfall_id=BOLT_ID, set_code="M11", quantity=10)
    overlaps = find_overlap([item], [listing_a, listing_b])

    client = MagicMock()
    result = apply_force_remove(overlaps, client, path)

    # listing_a fully consumed (3), listing_b trimmed by the remaining 2.
    client.delete_seller_listing.assert_called_once_with(listing_a)
    client.update_seller_listing_price.assert_called_once_with(listing_b, listing_b.price_usd, 8)
    assert result.inventory_qty_removed == 5
    assert load_buylist(path) == []


def test_force_remove_skips_buylist_decrement_when_inventory_removal_fails(tmp_path):
    """If the API call to delist fails, the buy list entry must not be dropped for
    stock that's still sitting in inventory."""
    item = make_item(card_name="Lightning Bolt", scryfall_id=BOLT_ID, target_quantity=4)
    path = _buylist_with(tmp_path, item)
    listing = make_seller_listing(scryfall_id=BOLT_ID, quantity=4)
    overlaps = find_overlap([item], [listing])

    client = MagicMock()
    client.delete_seller_listing.side_effect = RuntimeError("API down")
    result = apply_force_remove(overlaps, client, path)

    assert result.inventory_qty_removed == 0
    assert len(result.removal_errors) == 1
    assert result.buylist_decremented == []

    remaining = load_buylist(path)
    assert len(remaining) == 1
    assert remaining[0].target_quantity == 4
