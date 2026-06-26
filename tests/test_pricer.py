"""Unit tests for the seller inventory pricing engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from manabot.api.manapool_catalog import CatalogVariant
from manabot.models import Condition, Finish
from manabot.pricer import (
    HARD_FLOOR_USD,
    PricingConfig,
    PriceRecommendation,
    _project_price,
    compute_price,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _variant(
    scryfall_id: str = "abc-123",
    condition_id: str = "NM",
    finish_id: str = "NF",
    language_id: str = "EN",
    low_price_usd: float = 0.0,
    recent_sales: list[dict] | None = None,
    market_price_usd: float | None = None,
) -> CatalogVariant:
    return CatalogVariant(
        scryfall_id=scryfall_id,
        card_name="Test Card",
        set_code="TST",
        condition_id=condition_id,
        finish_id=finish_id,
        language_id=language_id,
        low_price_usd=low_price_usd,
        available_quantity=5,
        recent_sales=recent_sales or [],
        market_price_usd=market_price_usd,
    )


def _sale(price_cents: int, days_ago: float) -> dict:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"created_at": dt.isoformat(), "price": price_cents, "quantity": 1}


def _compute(
    variant: CatalogVariant | None,
    current_price: float = 5.00,
    cost_basis: float | None = None,
    days_below: int = 0,
    config: PricingConfig | None = None,
) -> PriceRecommendation:
    return compute_price(
        listing_scryfall_id="abc-123",
        listing_card_name="Test Card",
        listing_set_code="TST",
        listing_condition=Condition.NM,
        listing_finish=Finish.NONFOIL,
        listing_language="EN",
        listing_current_price_usd=current_price,
        catalog_variant=variant,
        cost_basis_usd=cost_basis,
        days_below_floor=days_below,
        config=config or PricingConfig(),
    )


DEFAULT_CONFIG = PricingConfig(
    race_to_bottom_threshold=0.20,
    min_margin_pct=0.10,
    cost_floor_days=30,
)


# ---------------------------------------------------------------------------
# Tests: no data
# ---------------------------------------------------------------------------

def test_none_catalog_variant_returns_no_change():
    rec = _compute(None, current_price=3.00)
    assert rec.reason == "no_data"
    assert rec.should_update is False
    assert rec.new_price_usd == 3.00
    assert rec.trend_target_usd is None


def test_no_sales_no_listings_returns_no_change():
    v = _variant(low_price_usd=0.0, recent_sales=[])
    rec = _compute(v, current_price=3.00)
    assert rec.reason == "no_data"
    assert rec.should_update is False
    assert rec.trend_target_usd is None


# ---------------------------------------------------------------------------
# Tests: regression-based pricing with low_price
# ---------------------------------------------------------------------------

def test_beat_low_price_by_one_cent():
    """5 flat sales at $5, low=$4.99 — regression projects ~$5, beats low by 1 cent."""
    sales = [_sale(500, days_ago=i * 2) for i in range(5)]
    v = _variant(low_price_usd=4.99, recent_sales=sales)
    rec = _compute(v, current_price=6.00, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_beat_low"
    assert rec.new_price_usd == pytest.approx(4.98, abs=0.02)
    assert rec.trend_target_usd == pytest.approx(4.98, abs=0.02)


def test_race_to_bottom_holds_at_projection():
    """Sales trend ~$5, low=$2.00 (60% below projection threshold) — price at projection."""
    sales = [_sale(500, days_ago=i * 3) for i in range(10)]
    v = _variant(low_price_usd=2.00, recent_sales=sales)
    rec = _compute(v, current_price=6.00, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_race_to_bottom"
    # Should be near $5.00, not $1.99
    assert rec.new_price_usd > 3.00
    assert rec.new_price_usd == pytest.approx(rec.trend_target_usd, abs=0.01)


def test_flat_trend_no_listings():
    """5 sales all at $5, no low_price — project to ~$5."""
    sales = [_sale(500, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=6.00, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_no_listings"
    assert rec.trend_target_usd == pytest.approx(5.00, abs=0.10)
    assert rec.new_price_usd == pytest.approx(5.00, abs=0.10)


def test_upward_trend_projected_to_today():
    """10 sales increasing from $3→$6 over 30 days — today's projection > $5."""
    n = 10
    sales = [_sale(int((300 + i * 33)), days_ago=(n - i - 1) * 3) for i in range(n)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=3.00, config=DEFAULT_CONFIG)
    assert rec.trend_target_usd is not None
    assert rec.trend_target_usd > 5.00


def test_downward_trend_projected_to_today():
    """10 sales decreasing from $6→$3 over 30 days — today's projection < $4."""
    n = 10
    sales = [_sale(int((600 - i * 33)), days_ago=(n - i - 1) * 3) for i in range(n)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=7.00, config=DEFAULT_CONFIG)
    assert rec.trend_target_usd is not None
    assert rec.trend_target_usd < 4.00


# ---------------------------------------------------------------------------
# Tests: cost floor (trend_target preserved, new_price floored)
# ---------------------------------------------------------------------------

def test_cost_floor_applied_and_trend_target_preserved():
    """trend_target=$2.00, cost=$3.00, margin=10% → new_price=$3.30, trend_target stays $2.00."""
    sales = [_sale(200, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=4.00, cost_basis=3.00, config=DEFAULT_CONFIG)
    assert rec.reason == "cost_floor"
    assert rec.new_price_usd == pytest.approx(3.30, abs=0.01)
    assert rec.trend_target_usd == pytest.approx(2.00, abs=0.10)


def test_cost_floor_lifted_after_days_exceeded():
    """days_below=31 > cost_floor_days=30 — floor does not apply, uses trend target."""
    sales = [_sale(200, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=4.00, cost_basis=3.00, days_below=31, config=DEFAULT_CONFIG)
    assert rec.reason != "cost_floor"
    assert rec.new_price_usd == pytest.approx(rec.trend_target_usd or 0, abs=0.01)


# ---------------------------------------------------------------------------
# Tests: hard floor
# ---------------------------------------------------------------------------

def test_hard_floor_applied():
    """Trend projects to ~$0.05 — hard floor of $0.15 enforced."""
    sales = [_sale(5, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=1.00, config=DEFAULT_CONFIG)
    assert rec.reason == "hard_floor"
    assert rec.new_price_usd == HARD_FLOOR_USD


def test_trend_target_unaffected_by_hard_floor():
    """Hard floor affects new_price_usd but trend_target_usd stays at the projected value."""
    sales = [_sale(5, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=1.00, config=DEFAULT_CONFIG)
    assert rec.new_price_usd == HARD_FLOOR_USD
    assert rec.trend_target_usd is not None
    assert rec.trend_target_usd < HARD_FLOOR_USD  # trend was below the floor


# ---------------------------------------------------------------------------
# Tests: single-sale fallback
# ---------------------------------------------------------------------------

def test_single_sale_no_low_price():
    """One sale at $4.00, no listings — single-sale fallback returns $4.00."""
    v = _variant(low_price_usd=0.0, recent_sales=[_sale(400, days_ago=5)])
    rec = _compute(v, current_price=5.00, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_no_listings"
    assert rec.trend_target_usd == pytest.approx(4.00, abs=0.01)


def test_single_sale_with_competitive_low():
    """One sale at $4.00, low=$3.50 — beat low by 1 cent → $3.49."""
    v = _variant(low_price_usd=3.50, recent_sales=[_sale(400, days_ago=5)])
    rec = _compute(v, current_price=5.00, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_beat_low"
    assert rec.new_price_usd == pytest.approx(3.49, abs=0.01)


def test_single_outlier_sale_does_not_override_market_low():
    """Single high-priced outlier sale must not trigger race-to-bottom guard.

    Regression has < 3 sales so it falls to the single-sale path.  The lone
    sale ($1.19) should not cause us to hold at $1.19 when the market low is
    $0.25 — we should follow the low instead.
    """
    v = _variant(low_price_usd=0.25, recent_sales=[_sale(119, days_ago=20)])
    rec = _compute(v, current_price=1.19, config=DEFAULT_CONFIG)
    assert rec.reason == "trend_beat_low"
    assert rec.new_price_usd == pytest.approx(0.24, abs=0.01)
    assert rec.new_price_usd < 1.00  # must not hold at the outlier sale price


# ---------------------------------------------------------------------------
# Tests: should_update flag
# ---------------------------------------------------------------------------

def test_should_update_true_when_price_differs():
    """New price differs from current by >= $0.01 → should_update True."""
    sales = [_sale(500, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=10.00, config=DEFAULT_CONFIG)
    assert abs(rec.new_price_usd - 10.00) >= 0.01
    assert rec.should_update is True


def test_should_update_false_when_price_same():
    """If computed target matches current price within $0.005 → should_update False."""
    # 5 sales all at exactly our current price of $5.00, no low_price
    sales = [_sale(500, days_ago=i * 5) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute(v, current_price=5.00, config=DEFAULT_CONFIG)
    # projection of flat $5 sales = ~$5.00 → should_update False
    assert rec.should_update is False


# ---------------------------------------------------------------------------
# TCGPlayer market integration
# ---------------------------------------------------------------------------

def _compute_tcg(
    variant: CatalogVariant | None,
    tcg_market: float | None,
    current_price: float = 5.00,
    config: PricingConfig | None = None,
) -> PriceRecommendation:
    return compute_price(
        listing_scryfall_id="abc-123",
        listing_card_name="Test Card",
        listing_set_code="TST",
        listing_condition=Condition.NM,
        listing_finish=Finish.NONFOIL,
        listing_language="EN",
        listing_current_price_usd=current_price,
        catalog_variant=variant,
        cost_basis_usd=None,
        days_below_floor=0,
        config=config or DEFAULT_CONFIG,
        tcg_market_usd=tcg_market,
    )


def test_tcg_market_used_for_no_listings_regardless_of_projection():
    """When no ManaPool listings exist, TCGPlayer market is used directly.

    TCGPlayer has deeper transaction volume than a sparse ManaPool projection,
    so we trust it whether the projection is above OR below TCGPlayer market.
    """
    # Projection trends up to ~$9 but TCGPlayer says $5 — use $5
    sales = [_sale(int(500 + i * 100), days_ago=(4 - i) * 10) for i in range(5)]
    v = _variant(low_price_usd=0.0, recent_sales=sales)
    rec = _compute_tcg(v, tcg_market=5.00)
    assert rec.reason == "tcg_market"
    assert rec.new_price_usd == pytest.approx(5.00, abs=0.01)
    assert rec.tcg_market_usd == 5.00

    # Projection is only $0.34 but TCGPlayer says $1.00 — use $1.00, not $0.34
    sparse_sales = [_sale(34, days_ago=20)]  # single recent sale at $0.34
    v2 = _variant(low_price_usd=0.0, recent_sales=sparse_sales)
    rec2 = _compute_tcg(v2, tcg_market=1.00)
    assert rec2.reason == "tcg_market"
    assert rec2.new_price_usd == pytest.approx(1.00, abs=0.01)


def test_tcg_market_not_used_when_manapool_listings_exist():
    """When ManaPool has competing listings, existing race-to-bottom logic applies."""
    sales = [_sale(500, days_ago=i * 10) for i in range(5)]
    v = _variant(low_price_usd=4.50, recent_sales=sales)
    rec = _compute_tcg(v, tcg_market=3.00)  # TCG lower but ManaPool listing exists
    assert rec.reason in ("trend_beat_low", "trend_race_to_bottom")


def test_tcg_market_used_when_no_catalog_variant():
    """When catalog has no entry at all, TCGPlayer market is the pricing source."""
    rec = _compute_tcg(None, tcg_market=4.25, current_price=5.00)
    assert rec.reason == "tcg_market"
    assert rec.new_price_usd == 4.25
    assert rec.should_update is True


def test_no_data_without_tcg_or_catalog():
    """No catalog variant and no TCGPlayer data → no_data, price unchanged."""
    rec = _compute_tcg(None, tcg_market=None, current_price=5.00)
    assert rec.reason == "no_data"
    assert rec.should_update is False
