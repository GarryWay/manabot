from datetime import datetime, timezone

import pytest

from manabot.matcher import match
from manabot.models import (
    BuyListItem,
    Condition,
    Finish,
    MatchStatus,
    PriceListing,
)

NOW = datetime.now(timezone.utc)
BOLT_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"


def make_listing(
    scryfall_id=BOLT_ID,
    name="Lightning Bolt",
    set_code="LEB",
    condition=Condition.NM,
    finish=Finish.NONFOIL,
    price=1.25,
    qty=4,
) -> PriceListing:
    return PriceListing(
        scryfall_id=scryfall_id,
        card_name=name,
        set_code=set_code,
        condition=condition,
        finish=finish,
        price_usd=price,
        quantity_available=qty,
        seller_id="seller1",
        fetched_at=NOW,
    )


def make_item(
    card_name="Lightning Bolt",
    scryfall_id=BOLT_ID,
    target_quantity=4,
    max_price=2.00,
    min_condition=Condition.LP,
    foil=Finish.ANY,
    allowed_sets=None,
    in_universe_only=False,
    tags=None,
) -> BuyListItem:
    return BuyListItem(
        card_name=card_name,
        scryfall_id=scryfall_id,
        target_quantity=target_quantity,
        max_price_usd=max_price,
        min_condition=min_condition,
        foil=foil,
        allowed_sets=allowed_sets or [],
        in_universe_only=in_universe_only,
        tags=tags or [],
    )


# --- ID matching ---

def test_match_by_scryfall_id():
    listings = [make_listing()]
    results = match([make_item()], listings)
    assert results[0].status == MatchStatus.MATCHED
    assert results[0].best_price == pytest.approx(1.25)


def test_match_by_name_when_no_id():
    item = make_item(scryfall_id=None)
    listings = [make_listing()]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED


def test_unresolved_when_no_listing():
    results = match([make_item()], [])
    assert results[0].status == MatchStatus.UNRESOLVED
    assert results[0].best_price is None


def test_unresolved_when_name_mismatch():
    item = make_item(card_name="Dark Ritual", scryfall_id=None)
    listings = [make_listing()]  # Lightning Bolt
    results = match([item], listings)
    assert results[0].status == MatchStatus.UNRESOLVED


# --- Condition filter ---

def test_condition_nm_passes_nm_requirement():
    item = make_item(min_condition=Condition.NM)
    listings = [make_listing(condition=Condition.NM)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED


def test_condition_lp_fails_nm_requirement():
    item = make_item(min_condition=Condition.NM)
    listings = [make_listing(condition=Condition.LP)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.UNRESOLVED


def test_condition_lp_passes_lp_requirement():
    item = make_item(min_condition=Condition.LP)
    listings = [make_listing(condition=Condition.LP)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED


def test_condition_hp_fails_mp_requirement():
    item = make_item(min_condition=Condition.MP)
    listings = [make_listing(condition=Condition.HP)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.UNRESOLVED


# --- Foil filter ---

def test_foil_filter_nonfoil():
    item = make_item(foil=Finish.NONFOIL)
    listings = [make_listing(finish=Finish.FOIL), make_listing(finish=Finish.NONFOIL, price=1.00)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED
    assert results[0].best_price == pytest.approx(1.00)


def test_foil_filter_foil_only():
    item = make_item(foil=Finish.FOIL)
    listings = [make_listing(finish=Finish.NONFOIL)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.UNRESOLVED


def test_foil_any_accepts_both():
    item = make_item(foil=Finish.ANY)
    listings = [make_listing(finish=Finish.FOIL, price=2.00), make_listing(finish=Finish.NONFOIL, price=1.00)]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED
    assert results[0].best_price == pytest.approx(1.00)


# --- Set filter ---

def test_allowed_sets_filters_correctly():
    item = make_item(allowed_sets=["LEA"], scryfall_id=None)
    listings = [
        make_listing(set_code="LEA", price=5.00),
        make_listing(set_code="LEB", price=1.00),
    ]
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED
    assert results[0].best_price == pytest.approx(5.00)


def test_allowed_sets_unresolved_when_no_match():
    item = make_item(allowed_sets=["LEA"], scryfall_id=None)
    listings = [make_listing(set_code="LEB")]
    results = match([item], listings)
    assert results[0].status == MatchStatus.UNRESOLVED


# --- Multiple listings ---

def test_multiple_listings_attached_and_best_picked():
    item = make_item()
    listings = [
        make_listing(price=2.00, qty=2),
        make_listing(price=0.75, qty=6),
        make_listing(price=1.25, qty=4),
    ]
    results = match([item], listings)
    assert len(results[0].listings) == 3
    assert results[0].best_price == pytest.approx(0.75)


# --- is_good_buy ---

def test_is_good_buy_true():
    item = make_item(max_price=2.00, target_quantity=4)
    listings = [make_listing(price=1.50, qty=4)]
    results = match([item], listings)
    assert results[0].is_good_buy is True


def test_is_good_buy_false_price_over():
    item = make_item(max_price=1.00)
    listings = [make_listing(price=1.50, qty=4)]
    results = match([item], listings)
    assert results[0].is_good_buy is False


def test_is_good_buy_false_quantity_short():
    item = make_item(target_quantity=8)
    listings = [make_listing(price=1.00, qty=2)]
    results = match([item], listings)
    assert results[0].is_good_buy is False


# --- In-universe filter ---

def test_in_universe_warns_when_no_scryfall_client():
    item = make_item(in_universe_only=True)
    listings = [make_listing()]
    results = match([item], listings, scryfall_client=None)
    assert results[0].status == MatchStatus.WARN_SCRYFALL_NEEDED


def test_name_normalization():
    item = make_item(card_name="Lightning Bolt", scryfall_id=None)
    listings = [make_listing(name="Lightning  Bolt")]  # extra space
    results = match([item], listings)
    assert results[0].status == MatchStatus.MATCHED
