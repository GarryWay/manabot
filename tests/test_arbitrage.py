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
    market: float | None = 2.00,
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
    """NM listing with price used as both price and market."""
    return _listing(condition=Condition.NM, price=price, market=price, **kwargs)


def _pair(nm_price=2.00, lp_price=1.00, **kwargs) -> list[PriceListing]:
    """NM + LP nonfoil for the same card/printing. market_price_usd = nm_price on both."""
    nm = _listing(condition=Condition.NM, price=nm_price, market=nm_price, **kwargs)
    lp = _listing(condition=Condition.LP, price=lp_price, market=nm_price, **kwargs)
    return [nm, lp]


# --- find_candidates: basic eligibility ---

def test_basic_candidate_found():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=5)
    assert len(candidates) == 1
    assert candidates[0].discount_pct == pytest.approx(50.0)


def test_candidate_at_market_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=2.00),
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_candidate_above_market_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=2.50),
                                 min_discount_pct=10.0, min_quantity=5)
    assert candidates == []


def test_below_min_discount_excluded():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.95),  # 2.5% below market
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


def test_mp_condition_excluded():
    nm = _nm(price=2.00)
    mp = _listing(condition=Condition.MP, price=1.00, market=2.00)
    candidates = find_candidates([nm, mp], min_discount_pct=10.0, min_quantity=5)
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


# --- market_price_usd as reference ---

def test_market_price_usd_used_as_reference_not_listing_price():
    """The discount baseline is market_price_usd, not another listing's price_usd.
    Both NM and LP qualify when priced below the card-level market."""
    nm = _listing(condition=Condition.NM, price=4.65, market=12.48)
    lp = _listing(condition=Condition.LP, price=1.45, market=12.48)
    candidates = find_candidates([nm, lp], min_discount_pct=10.0, min_quantity=1)
    # Both are below the card market ($12.48): LP at 88.4%, NM at 62.7% — sorted desc
    assert len(candidates) == 2
    assert candidates[0].listing.condition == Condition.LP  # 88.4% — best deal first
    assert candidates[0].market_price_usd == pytest.approx(12.48)
    assert candidates[0].discount_pct == pytest.approx((1 - 1.45 / 12.48) * 100)
    assert candidates[1].listing.condition == Condition.NM  # 62.7%
    assert candidates[1].market_price_usd == pytest.approx(12.48)


def test_listing_without_market_price_excluded():
    """Listings with market_price_usd=None are skipped — insufficient sales volume."""
    lp = _listing(condition=Condition.LP, price=1.00, market=None)
    candidates = find_candidates([lp], min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_card_with_no_market_data_excluded():
    """Cards where no listing has market_price_usd are excluded entirely."""
    nm = _listing(condition=Condition.NM, price=4.00, market=None)
    lp = _listing(condition=Condition.LP, price=1.00, market=None)
    candidates = find_candidates([nm, lp], min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


# --- Card-level ineligibility ---

def test_cheap_printing_makes_entire_card_ineligible():
    """Any printing's market price below the threshold marks the whole card as bulk."""
    expensive_lp = _listing(name="Maze's End", scryfall_id="m1", set_code="RTR",
                            condition=Condition.LP, price=2.00, market=2.44)
    cheap_foil = _listing(name="Maze's End", scryfall_id="m2", set_code="OTC",
                          condition=Condition.NM, finish=Finish.FOIL, price=0.15, market=0.15)
    candidates = find_candidates([expensive_lp, cheap_foil],
                                 min_discount_pct=10.0, min_quantity=1,
                                 min_market_price_usd=2.00)
    assert candidates == []  # cheap foil ($0.15 market) marks entire card ineligible


def test_cheap_reprint_deflates_card_market_for_all_printings():
    """A new cheap printing's market price becomes the card-level floor, making old-printing
    LP listings appear above market even when they look discounted vs the old printing."""
    nm_old = _listing(name="X", scryfall_id="x1", set_code="OLD",
                      condition=Condition.NM, price=12.48, market=12.48)
    nm_new = _listing(name="X", scryfall_id="x2", set_code="NEW",
                      condition=Condition.NM, price=4.65, market=4.65)
    lp_old_above_floor = _listing(name="X", scryfall_id="x1", set_code="OLD",
                                  condition=Condition.LP, price=8.00, market=12.48)
    candidates = find_candidates([nm_old, nm_new, lp_old_above_floor],
                                 min_discount_pct=10.0, min_quantity=1)
    # Card min market = $4.65 (NEW printing). Old LP at $8.00 > $4.65 → not a candidate.
    assert candidates == []


def test_old_printing_lp_below_card_market_is_candidate():
    """Old printing LP priced below the card-level market floor (from a cheaper printing) qualifies."""
    nm_new = _listing(name="X", scryfall_id="x2", set_code="NEW",
                      condition=Condition.NM, price=4.65, market=4.65)
    lp_old = _listing(name="X", scryfall_id="x1", set_code="OLD",
                      condition=Condition.LP, price=1.45, market=12.48)
    candidates = find_candidates([nm_new, lp_old], min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
    # Baseline is card-level min ($4.65 from NEW), not old printing's own market ($12.48)
    assert candidates[0].market_price_usd == pytest.approx(4.65)
    assert candidates[0].discount_pct == pytest.approx((1 - 1.45 / 4.65) * 100)


def test_lp_without_own_printing_market_still_qualifies_via_card_floor():
    """LP listing without a same-printing NM still qualifies if card_min_market is available."""
    nm_set1 = _nm(name="X", scryfall_id="x1", set_code="SET1", price=5.00)
    lp_set1 = _listing(name="X", scryfall_id="x1", set_code="SET1",
                        condition=Condition.LP, price=1.50, market=5.00)
    # SET2 has no NM listing, but LP has market data → still evaluated via card-level market
    lp_set2 = _listing(name="X", scryfall_id="x2", set_code="SET2",
                        condition=Condition.LP, price=1.00, market=5.00)
    candidates = find_candidates([nm_set1, lp_set1, lp_set2],
                                 min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 2  # both qualify via card-level market ($5.00)
    assert candidates[0].listing.set_code == "SET2"  # 80% discount first
    assert candidates[1].listing.set_code == "SET1"  # 70% discount second


# --- All finishes eligible as purchase targets ---

def test_foil_lp_is_a_valid_purchase_target():
    """Foil LP listings are valid purchase targets when priced below card-level market."""
    foil_lp = _listing(condition=Condition.LP, finish=Finish.FOIL, price=1.50, market=3.00)
    candidates = find_candidates([foil_lp], min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
    assert candidates[0].listing.finish == Finish.FOIL


def test_foil_lp_above_card_market_excluded():
    """Foil LP above the card-level market floor is not a candidate despite the foil premium."""
    nonfoil_nm = _listing(condition=Condition.NM, finish=Finish.NONFOIL, price=3.00, market=3.00)
    foil_lp = _listing(condition=Condition.LP, finish=Finish.FOIL, price=4.50, market=50.00)
    # Card min market = min(3.00, 50.00) = 3.00. Foil LP at $4.50 > $3.00 → not a candidate.
    candidates = find_candidates([nonfoil_nm, foil_lp], min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_cheap_foil_market_contributes_to_card_floor():
    """A cheap foil market price lowers the card floor, preventing inflated nonfoil LP from qualifying."""
    cheap_foil = _listing(condition=Condition.NM, finish=Finish.FOIL, price=0.50, market=0.50)
    nonfoil_lp = _listing(condition=Condition.LP, finish=Finish.NONFOIL, price=0.40, market=3.00)
    candidates = find_candidates([cheap_foil, nonfoil_lp],
                                 min_discount_pct=10.0, min_quantity=1,
                                 min_market_price_usd=2.00)
    # Card min market = min(0.50, 3.00) = $0.50 < $2.00 → entire card ineligible
    assert candidates == []


# --- NM as a purchase target ---

def test_nm_below_card_market_is_candidate():
    """NM listings priced below the card-level market floor are valid purchase targets."""
    nm_market = _listing(condition=Condition.NM, price=5.00, market=5.00)
    nm_cheap = _listing(condition=Condition.NM, price=3.00, market=5.00)
    # Card min market = min(5.00, 5.00) = 5.00. nm_cheap at $3.00 = 40% discount.
    candidates = find_candidates([nm_market, nm_cheap], min_discount_pct=10.0, min_quantity=1)
    assert len(candidates) == 1
    assert candidates[0].listing.price_usd == pytest.approx(3.00)
    assert candidates[0].market_price_usd == pytest.approx(5.00)


# --- BuyListItem fields ---

def test_buy_list_item_max_price_is_card_market():
    candidates = find_candidates(_pair(nm_price=2.50, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.max_price_usd == pytest.approx(2.50)


def test_buy_list_item_min_condition_is_lp():
    candidates = find_candidates(_pair(), min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.min_condition == Condition.LP


def test_buy_list_item_finish_matches_candidate_finish():
    """BuyListItem.foil is set to the candidate listing's finish type."""
    nonfoil_lp = _listing(condition=Condition.LP, finish=Finish.NONFOIL, price=1.00, market=2.00)
    foil_lp = _listing(condition=Condition.LP, finish=Finish.FOIL, price=1.00, market=2.00)
    candidates_nf = find_candidates([nonfoil_lp], min_discount_pct=10.0, min_quantity=1)
    candidates_fo = find_candidates([foil_lp], min_discount_pct=10.0, min_quantity=1)
    assert candidates_nf[0].buy_list_item.foil == Finish.NONFOIL
    assert candidates_fo[0].buy_list_item.foil == Finish.FOIL


def test_no_set_code_constraint_on_buy_list_item():
    candidates = find_candidates(_pair(nm_price=2.00, lp_price=1.00),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates[0].buy_list_item.allowed_sets == []


# --- Sorting ---

def test_sorted_by_discount_descending():
    listings = [
        _listing(name="A", scryfall_id="a", condition=Condition.LP, price=1.50, market=2.00),  # 25%
        _listing(name="B", scryfall_id="b", condition=Condition.LP, price=1.00, market=2.00),  # 50%
        _listing(name="C", scryfall_id="c", condition=Condition.LP, price=1.80, market=2.00),  # 10%
    ]
    candidates = find_candidates(listings, min_discount_pct=5.0, min_quantity=1)
    assert [c.listing.card_name for c in candidates] == ["B", "A", "C"]


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
    nm = _listing(name="Planequake", condition=Condition.NM, price=11.99, market=11.99)
    lp = _listing(name="Planequake", condition=Condition.LP, price=0.93, market=11.99)
    candidates = find_candidates([nm, lp], scryfall=_mock_scryfall(sanctioned=False),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_token_card_excluded():
    nm = _listing(name="Stoneforged Blade // Germ", condition=Condition.NM, price=5.00, market=5.00)
    lp = _listing(name="Stoneforged Blade // Germ", condition=Condition.LP, price=1.00, market=5.00)
    candidates = find_candidates([nm, lp], scryfall=_mock_scryfall(token=True),
                                 min_discount_pct=10.0, min_quantity=1)
    assert candidates == []


def test_new_set_card_excluded():
    nm = _listing(name="Wakandan Tusker", set_code="MSH", condition=Condition.NM, price=5.00, market=5.00)
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
