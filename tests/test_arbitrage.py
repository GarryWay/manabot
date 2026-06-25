from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from manabot.arbitrage import ArbitrageCandidate, find_candidates, candidates_to_match_results
from manabot.models import Condition, Finish, MatchStatus, PriceListing

NOW = datetime.now(timezone.utc)


def _listing(
    name="Lightning Bolt",
    scryfall_id="abc123",
    set_code="M11",
    condition=Condition.LP,
    finish=Finish.NONFOIL,
    price=1.00,
    market=2.00,
    qty=10,
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
        market_price_usd=market,
    )


def _nm(price=2.00, **kwargs) -> PriceListing:
    """Convenience: NM listing with price used as both price and market."""
    return _listing(condition=Condition.NM, price=price, market=price, **kwargs)


def _pair(nm_price=2.00, lp_price=1.00, **kwargs) -> list[PriceListing]:
    """NM + LP listing for the same card/printing — standard test fixture."""
    nm = _listing(condition=Condition.NM, price=nm_price, market=nm_price, **kwargs)
    lp = _listing(condition=Condition.LP, price=lp_price, market=nm_price, **kwargs)
    return [nm, lp]


# --- find_candidates: basic filtering ---

def test_basic_candidate_found():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=5)
    assert len(candidates) == 1
    assert candidates[0].discount_pct == pytest.approx(50.0)


def test_no_nm_listing_no_candidate():
    """Without a live NM reference, LP listings are not evaluated."""
    lp = _listing(condition=Condition.LP, price=1.00, market=2.00)
    candidates = find_candidates([lp], min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_candidate_at_market_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=2.00),
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_candidate_above_market_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=2.50),
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_below_min_discount_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.95),  # 2.5% below
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_exactly_at_min_discount_included():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.80),  # exactly 10%
                                 min_discount_pct=10.0, min_quantity=5)
    assert len(candidates) == 1


def test_below_min_quantity_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00, qty=3),
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_foil_listing_excluded():
    nm = _nm(price=2.00)
    foil_lp = _listing(condition=Condition.LP, price=1.00, finish=Finish.FOIL)
    candidates = find_candidates([nm, foil_lp], min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_mp_condition_excluded():
    nm = _nm(price=2.00)
    mp = _listing(condition=Condition.MP, price=1.00)
    candidates = find_candidates([nm, mp], min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_nm_listing_is_not_a_candidate():
    """NM listings set the market floor; they are not arbitrage targets themselves."""
    nm = _nm(price=1.00)
    candidates = find_candidates([nm], min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_below_min_market_price_excluded():
    candidates = find_candidates(_pair(nm_price=1.50, lp_price=0.50),
                                 min_discount_pct=10.0, min_quantity=1,
                                 min_market_price_usd=2.00)
    assert candidates == []


def test_at_min_market_price_included():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=1,
                                 min_market_price_usd=2.00)
    assert len(candidates) == 1


# --- Cross-printing logic ---

def test_cheapest_nm_across_printings_is_market_reference():
    """Market reference is the cheapest NM across all printings, not the LP's printing."""
    nm_old = _listing(name="X", scryfall_id="x1", set_code="OLD",
                      condition=Condition.NM, price=12.48, market=12.48)
    nm_new = _listing(name="X", scryfall_id="x2", set_code="NEW",
                      condition=Condition.NM, price=4.65, market=4.65)
    lp_old = _listing(name="X", scryfall_id="x1", set_code="OLD",
                      condition=Condition.LP, price=1.45, market=12.48)
    candidates = find_candidates([nm_old, nm_new, lp_old],
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
    assert candidates[0].market_price_usd == pytest.approx(4.65)  # cheapest NM, not $12.48


def test_lp_from_any_printing_is_candidate():
    """LP from a more expensive printing is still a candidate if cheaper than NM floor."""
    nm_cheap = _listing(name="X", scryfall_id="x2", set_code="NEW",
                        condition=Condition.NM, price=4.65, market=4.65)
    lp_old = _listing(name="X", scryfall_id="x1", set_code="OLD",
                      condition=Condition.LP, price=1.45, market=12.48)
    candidates = find_candidates([nm_cheap, lp_old], min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1


def test_all_lp_printings_are_candidates():
    """All LP listings below the NM floor qualify — no deduplication by card name."""
    nm = _nm(name="X", scryfall_id="x1", set_code="SET1", price=5.00)
    lp_expensive = _listing(name="X", scryfall_id="x1", set_code="SET1",
                             condition=Condition.LP, price=1.50, market=5.00)
    lp_cheap = _listing(name="X", scryfall_id="x2", set_code="SET2",
                        condition=Condition.LP, price=1.00, market=5.00)
    candidates = find_candidates([nm, lp_expensive, lp_cheap],
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 2
    # Sorted by discount descending: SET2 (80%) before SET1 (70%)
    assert candidates[0].listing.price_usd == pytest.approx(1.00)
    assert candidates[1].listing.price_usd == pytest.approx(1.50)


def test_no_set_code_constraint_on_buy_list_item():
    """Optimizer is unconstrained — optimizer finds cheapest printing, allowed_sets is empty."""
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.allowed_sets == []


# --- NM price as market reference ---

def test_nm_price_used_as_market_reference():
    """LP is evaluated against the live NM price, not stale price_market."""
    nm = _listing(condition=Condition.NM, price=4.65, market=12.48)
    lp = _listing(condition=Condition.LP, price=1.45, market=12.48)
    candidates = find_candidates([nm, lp], min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
    assert candidates[0].market_price_usd == pytest.approx(4.65)
    assert candidates[0].discount_pct == pytest.approx((1 - 1.45 / 4.65) * 100)


# --- Sorting ---

def test_sorted_by_discount_descending():
    listings = [
        _nm(name="A", scryfall_id="a", price=2.00),
        _listing(name="A", scryfall_id="a", condition=Condition.LP, price=1.50, market=2.00),  # 25%
        _nm(name="B", scryfall_id="b", price=2.00),
        _listing(name="B", scryfall_id="b", condition=Condition.LP, price=1.00, market=2.00),  # 50%
        _nm(name="C", scryfall_id="c", price=2.00),
        _listing(name="C", scryfall_id="c", condition=Condition.LP, price=1.80, market=2.00),  # 10%
    ]
    candidates = find_candidates(listings, min_discount_pct=5.0, min_quantity=1)
    assert [c.listing.card_name for c in candidates] == ["B", "A", "C"]


# --- BuyListItem fields ---

def test_buy_list_item_max_price_is_nm_floor():
    candidates = find_candidates(_pair(nm_price=2.50, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.max_price_usd == pytest.approx(2.50)


def test_buy_list_item_min_condition_is_lp():
    candidates = find_candidates(_pair(), min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.min_condition == Condition.LP


def test_buy_list_item_finish_is_nonfoil():
    candidates = find_candidates(_pair(), min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.foil == Finish.NONFOIL


# --- candidates_to_match_results ---

def test_candidates_to_match_results_status():
    candidates = find_candidates(_pair(), min_discount_pct=10.0, min_quantity=1)
    results = candidates_to_match_results(candidates)
    assert len(results) == 1
    assert results[0].status == MatchStatus.MATCHED


def test_candidates_to_match_results_best_price():
    candidates = find_candidates(_pair(lp_price=1.23), min_discount_pct=10.0, min_quantity=1)
    results = candidates_to_match_results(candidates)
    assert results[0].best_price == pytest.approx(1.23)


# --- Sanctioned filtering ---

def _mock_scryfall(sanctioned: bool = True, token: bool = False, recently_released: bool = False):
    s = MagicMock()
    s.is_sanctioned.return_value = sanctioned
    s.is_token.return_value = token
    s.is_recently_released.return_value = recently_released
    return s


def test_sanctioned_card_included():
    candidates = find_candidates(_pair(), scryfall=_mock_scryfall(sanctioned=True),
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1


def test_unsanctioned_card_excluded():
    nm = _nm(name="Planequake", price=11.99)
    lp = _listing(name="Planequake", condition=Condition.LP, price=0.93, market=11.99)
    candidates = find_candidates([nm, lp], scryfall=_mock_scryfall(sanctioned=False),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_token_card_excluded():
    nm = _nm(name="Stoneforged Blade // Germ", price=5.00)
    lp = _listing(name="Stoneforged Blade // Germ", condition=Condition.LP, price=1.00, market=5.00)
    candidates = find_candidates([nm, lp], scryfall=_mock_scryfall(token=True),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_new_set_card_excluded():
    nm = _nm(name="Wakandan Tusker", set_code="MSH", price=5.00)
    lp = _listing(name="Wakandan Tusker", set_code="MSH", condition=Condition.LP, price=1.00, market=5.00)
    candidates = find_candidates([nm, lp], scryfall=_mock_scryfall(recently_released=True),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_old_set_card_included():
    candidates = find_candidates(_pair(), scryfall=_mock_scryfall(recently_released=False),
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1


def test_no_scryfall_does_not_filter():
    candidates = find_candidates(_pair(), scryfall=None,
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
