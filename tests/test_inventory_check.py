from manabot.inventory_check import find_overlap
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
