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


# ---------------------------------------------------------------------------
# Double-sided token upgrade detection
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch
from manabot.models import SellerListing
from manabot.pricer import apply_double_sided_upgrades, DoubleSidedUpgrade, PricingConfig


def _seller_listing(
    card_name: str,
    set_code: str,
    scryfall_id: str = "dft-123",
    inventory_id: str = "inv-001",
    product_id: str = "prod-001",
    condition: Condition = Condition.NM,
    finish: Finish = Finish.NONFOIL,
    price_usd: float = 0.25,
    quantity: int = 4,
    number: str = "",
) -> SellerListing:
    return SellerListing(
        inventory_id=inventory_id,
        product_id=product_id,
        scryfall_id=scryfall_id,
        card_name=card_name,
        set_code=set_code,
        condition=condition,
        finish=finish,
        language="EN",
        quantity=quantity,
        price_usd=price_usd,
        number=number,
    )


_TOKEN_VARIANT = [{"product_type": "mtg_token", "condition_id": "NM", "finish_id": "NF", "language_id": "EN"}]
_SINGLE_VARIANT = [{"product_type": "mtg_single", "condition_id": "NM", "finish_id": "NF", "language_id": "EN"}]


def test_double_sided_upgrade_detected_when_face_worth_more():
    """Single-sided face with higher market price triggers an upgrade."""
    inventory = [_seller_listing("Faerie Rogue // Thopter", "TBFZ", scryfall_id="dft-123")]
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Thopter"): {
            "scryfall_id": "single-789", "price_market": 15, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)

    assert len(upgrades) == 1
    assert upgrades[0].upgrade_name == "Faerie Rogue"
    assert upgrades[0].upgrade_scryfall_id == "single-456"
    assert upgrades[0].single_suggested_usd == pytest.approx(1.20)
    assert upgrades[0].dft_suggested_usd == pytest.approx(0.25)  # listing.price_usd default
    client.delete_seller_listing.assert_not_called()
    client.create_seller_listing.assert_not_called()


def test_double_sided_upgrade_picks_best_face():
    """When both faces are worth more than the DFT, the higher-priced face wins."""
    inventory = [_seller_listing("Spirit // Soldier", "TMOC", scryfall_id="dft-abc")]
    name_index = {
        ("TMOC", "Spirit // Soldier"): {
            "scryfall_id": "dft-abc", "price_market": 10, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TMOC", "Spirit"): {
            "scryfall_id": "spirit-id", "price_market": 50, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TMOC", "Soldier"): {
            "scryfall_id": "soldier-id", "price_market": 80, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)

    assert len(upgrades) == 1
    assert upgrades[0].upgrade_name == "Soldier"
    assert upgrades[0].upgrade_scryfall_id == "soldier-id"


def test_double_sided_no_upgrade_when_face_cheaper():
    """If neither face's suggested price exceeds the DFT's listing price, no upgrade."""
    # DFT listed at $2.00 (the price we'd lose by delisting it); face market=$1.20
    inventory = [_seller_listing("Faerie Rogue // Thopter", "TBFZ", scryfall_id="dft-123", price_usd=2.00)]
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 200, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)
    assert len(upgrades) == 0


def test_double_sided_upgrade_live_calls_delete_then_create():
    """In live mode, delete the DFT listing then create the single-sided listing."""
    listing = _seller_listing(
        "Faerie Rogue // Thopter", "TBFZ",
        scryfall_id="dft-123", inventory_id="inv-abc",
        quantity=3, price_usd=0.20,
    )
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades([listing], name_index, {}, PricingConfig(), client, dry_run=False)

    assert len(upgrades) == 1
    client.delete_seller_listing.assert_called_once_with(listing)
    client.create_seller_listing.assert_called_once_with(
        scryfall_id="single-456",
        condition=Condition.NM,
        finish=Finish.NONFOIL,
        price_usd=pytest.approx(1.20),
        quantity=3,
        language="EN",
    )


def test_double_sided_no_face_in_same_set():
    """If neither face has a catalog entry in the same set, no upgrade."""
    inventory = [_seller_listing("Faerie Rogue // Thopter", "TBFZ", scryfall_id="dft-123")]
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        # Faerie Rogue exists only in a different set
        ("TZNC", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)
    assert len(upgrades) == 0


def test_single_sided_cards_skipped():
    """Non-DFT listings are ignored by the upgrade check."""
    inventory = [_seller_listing("Lightning Bolt", "TST", scryfall_id="bolt-123")]
    name_index = {
        ("TST", "Lightning Bolt"): {"scryfall_id": "bolt-123", "price_market": 500, "variants": _SINGLE_VARIANT},
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)
    assert len(upgrades) == 0


def test_mdfc_spell_skipped_not_a_token():
    """MDFC spells (Sea Gate Restoration // Sea Gate, Reborn) must not be relisted."""
    inventory = [_seller_listing("Sea Gate Restoration // Sea Gate, Reborn", "ZNR", scryfall_id="mdfc-999")]
    name_index = {
        ("ZNR", "Sea Gate Restoration // Sea Gate, Reborn"): {
            "scryfall_id": "mdfc-999", "price_market": 4200, "price_market_foil": None,
            "variants": _SINGLE_VARIANT,  # <-- mtg_single, not mtg_token
        },
        ("ZNR", "Sea Gate Restoration"): {
            "scryfall_id": "face-111", "price_market": 5000, "price_market_foil": None,
            "variants": _SINGLE_VARIANT,
        },
    }
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(inventory, name_index, {}, PricingConfig(), client, dry_run=True)
    assert len(upgrades) == 0
    client.delete_seller_listing.assert_not_called()


def test_double_sided_upgrade_restores_listing_when_create_fails():
    """Regression: production hit an HTTP 405 creating the single-sided listing after
    the DFT was already deleted, silently losing the stock ('LISTING LOST'). Since
    delete_seller_listing() is just a PUT that zeroes price/quantity (ManaPool has no
    real DELETE endpoint), a failed create must restore the original DFT listing via
    that same PUT mechanism rather than leaving the stock gone."""
    listing = _seller_listing(
        "Faerie Rogue // Thopter", "TBFZ",
        scryfall_id="dft-123", inventory_id="inv-abc",
        quantity=3, price_usd=0.20,
    )
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    client.create_seller_listing.side_effect = Exception("HTTP 405 Method Not Allowed")

    upgrades = apply_double_sided_upgrades([listing], name_index, {}, PricingConfig(), client, dry_run=False)

    assert len(upgrades) == 1
    client.delete_seller_listing.assert_called_once_with(listing)
    client.create_seller_listing.assert_called_once()
    # Restore call puts the DFT listing back at its original price/quantity, not lost.
    client.update_seller_listing_price.assert_called_once_with(listing, listing.price_usd, listing.quantity)


def test_double_sided_upgrade_logs_manual_recovery_when_restore_also_fails():
    """If even the restore PUT fails, the listing really is unrecoverable automatically
    -- must not raise (would crash the whole pricing run over one card) and must not
    claim success; the 'manual recovery' log path is the last resort, not the default."""
    listing = _seller_listing(
        "Faerie Rogue // Thopter", "TBFZ",
        scryfall_id="dft-123", inventory_id="inv-abc",
        quantity=3, price_usd=0.20,
    )
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()
    client.create_seller_listing.side_effect = Exception("HTTP 405 Method Not Allowed")
    client.update_seller_listing_price.side_effect = Exception("restore also failed")

    # Must not raise -- one card's total failure shouldn't kill the whole pricing run.
    upgrades = apply_double_sided_upgrades([listing], name_index, {}, PricingConfig(), client, dry_run=False)
    assert len(upgrades) == 1
    client.update_seller_listing_price.assert_called_once_with(listing, listing.price_usd, listing.quantity)


def test_double_sided_upgrade_restores_all_merged_dfts_on_update_failure():
    """When multiple DFT listings consolidate into one target and the final
    update_seller_listing_price (merging into an existing target listing) fails, every
    deleted DFT in the group must be restored, not just one."""
    listing_a = _seller_listing(
        "Faerie Rogue // Thopter", "TBFZ", scryfall_id="dft-123",
        inventory_id="inv-a", product_id="prod-a", quantity=2, price_usd=0.20,
    )
    listing_b = _seller_listing(
        "Faerie Rogue // Thopter", "TBFZ", scryfall_id="dft-123",
        inventory_id="inv-b", product_id="prod-b", quantity=5, price_usd=0.18,
    )
    existing_target = _seller_listing(
        "Faerie Rogue", "TBFZ", scryfall_id="single-456",
        inventory_id="inv-existing", product_id="prod-existing", quantity=10, price_usd=1.00,
    )
    name_index = {
        ("TBFZ", "Faerie Rogue // Thopter"): {
            "scryfall_id": "dft-123", "price_market": 20, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
        ("TBFZ", "Faerie Rogue"): {
            "scryfall_id": "single-456", "price_market": 120, "price_market_foil": None,
            "variants": _TOKEN_VARIANT,
        },
    }
    client = MagicMock()

    def _update_side_effect(target_listing, price, qty):
        if target_listing is existing_target:
            raise Exception("HTTP 500")
        return None  # deletes (also update_seller_listing_price under the hood) succeed

    client.update_seller_listing_price.side_effect = _update_side_effect

    upgrades = apply_double_sided_upgrades(
        [listing_a, listing_b, existing_target], name_index, {}, PricingConfig(), client, dry_run=False,
    )

    assert len(upgrades) == 2
    # Both DFTs restored to their own original price/quantity.
    restore_calls = [
        c for c in client.update_seller_listing_price.call_args_list
        if c.args[0] is listing_a or c.args[0] is listing_b
    ]
    restored_ids = {id(c.args[0]) for c in restore_calls}
    assert restored_ids == {id(listing_a), id(listing_b)}
    for c in restore_calls:
        listing_arg = c.args[0]
        assert c.args[1] == listing_arg.price_usd
        assert c.args[2] == listing_arg.quantity


# ---------------------------------------------------------------------------
# Double-sided upgrade: reused token-sheet names (compound collector numbers)
#
# Regression for a real production incident: a token sheet ("Edge of Eternities
# Tokens") reuses the generic name "Lander" across five distinct printings (numbers
# 4/5/6/7/8), each pairing with different front faces. build_name_index() collapses
# same-name records to one arbitrary "winner", so every "* // Lander" DFT listing was
# resolving to the SAME wrong scryfall_id regardless of which Lander it actually
# printed with -- multiple unrelated DFTs got merged into one target_group, and the
# reported quantity (deleted_qty, summed across the whole wrongly-merged group) didn't
# match any single card the seller could point to. The DFT listing's own scryfall_id
# is no help either -- ManaPool sets it to the FRONT face's own id, carrying no
# information about which back face it's paired with. The fix: use the listing's
# compound `number` field (e.g. "2-7") to resolve each face via number_index instead
# -- collector numbers ARE unique per printing, unlike name.
# ---------------------------------------------------------------------------

def _lander_number_index():
    """Five distinct 'Lander' printings sharing one name, real TEOE numbers/scryfall_ids."""
    return {
        ("TEOE", "4"): {"name": "Lander", "scryfall_id": "lander-4", "price_market": 40, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "5"): {"name": "Lander", "scryfall_id": "lander-5", "price_market": 50, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "6"): {"name": "Lander", "scryfall_id": "lander-6", "price_market": 60, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "7"): {"name": "Lander", "scryfall_id": "lander-7", "price_market": 70, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "8"): {"name": "Lander", "scryfall_id": "lander-8", "price_market": 80, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "2"): {"name": "Human Soldier", "scryfall_id": "human-soldier-2", "price_market": 5, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "10"): {"name": "Robot", "scryfall_id": "robot-10", "price_market": 5, "price_market_foil": None, "variants": _TOKEN_VARIANT},
    }


def _lander_name_index_bug():
    """What build_name_index() actually produces for this data: one arbitrary
    'winner' per name (highest market price -- Lander #8 here), discarding the rest.
    Used to prove the fallback path alone reproduces the bug; number_index fixes it."""
    return {
        ("TEOE", "Lander"): {"name": "Lander", "scryfall_id": "lander-8", "price_market": 80, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "Human Soldier"): {"name": "Human Soldier", "scryfall_id": "human-soldier-2", "price_market": 5, "price_market_foil": None, "variants": _TOKEN_VARIANT},
        ("TEOE", "Robot"): {"name": "Robot", "scryfall_id": "robot-10", "price_market": 5, "price_market_foil": None, "variants": _TOKEN_VARIANT},
    }


def test_double_sided_upgrade_resolves_correct_printing_via_compound_number():
    """Three distinct 'Human Soldier // Lander' printings (numbers 2-4, 2-7, 2-8) all
    share the same (front-face) scryfall_id -- number_index must resolve each to its
    own distinct Lander, not collapse them to one."""
    listing_2_4 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-2-4", product_id="prod-2-4", number="2-4", quantity=1, price_usd=0.15,
    )
    listing_2_7 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-2-7", product_id="prod-2-7", number="2-7", quantity=2, price_usd=0.15,
    )
    listing_2_8 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-2-8", product_id="prod-2-8", number="2-8", quantity=1, price_usd=0.15,
    )
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(
        [listing_2_4, listing_2_7, listing_2_8],
        _lander_name_index_bug(), {}, PricingConfig(), client, dry_run=True,
        number_index=_lander_number_index(),
    )

    assert len(upgrades) == 3
    by_inventory_id = {u.listing.inventory_id: u for u in upgrades}
    assert by_inventory_id["inv-2-4"].upgrade_scryfall_id == "lander-4"
    assert by_inventory_id["inv-2-7"].upgrade_scryfall_id == "lander-7"
    assert by_inventory_id["inv-2-8"].upgrade_scryfall_id == "lander-8"
    # None of them collapsed to the name-index "winner" (lander-8) except the one
    # that's genuinely supposed to resolve there.
    assert by_inventory_id["inv-2-4"].upgrade_scryfall_id != "lander-8"
    assert by_inventory_id["inv-2-7"].upgrade_scryfall_id != "lander-8"


def test_double_sided_upgrade_without_number_index_reproduces_the_bug():
    """Sanity check that the test fixtures above actually model the real incident:
    without number_index, all three DFTs collapse onto the SAME wrong scryfall_id via
    the name-index fallback -- confirming the fix (not the test data) is what's load-bearing."""
    listing_2_4 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-2-4", number="2-4", quantity=1, price_usd=0.15,
    )
    listing_2_7 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-2-7", number="2-7", quantity=2, price_usd=0.15,
    )
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(
        [listing_2_4, listing_2_7], _lander_name_index_bug(), {}, PricingConfig(), client, dry_run=True,
        number_index=None,  # simulates the pre-fix call signature
    )
    # Both wrongly resolve to the same (arbitrary "highest price") Lander.
    assert {u.upgrade_scryfall_id for u in upgrades} == {"lander-8"}


def test_double_sided_upgrade_groups_by_resolved_printing_not_front_face():
    """'Human Soldier // Lander' (2-7) and 'Robot // Lander' (7-10) both genuinely
    resolve to the same physical Lander #7 -- they SHOULD consolidate into one target
    group (that's correct, not a bug), while a third listing targeting Lander #8 must
    stay in its own separate group."""
    human_soldier_2_7 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-hs-2-7", product_id="prod-hs-2-7", number="2-7", quantity=2, price_usd=0.15,
    )
    robot_7_10 = _seller_listing(
        "Robot // Lander", "TEOE", scryfall_id="robot-10",
        inventory_id="inv-robot-7-10", product_id="prod-robot-7-10", number="7-10", quantity=1, price_usd=0.15,
    )
    human_soldier_2_8 = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-hs-2-8", product_id="prod-hs-2-8", number="2-8", quantity=3, price_usd=0.15,
    )
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(
        [human_soldier_2_7, robot_7_10, human_soldier_2_8],
        _lander_name_index_bug(), {}, PricingConfig(), client, dry_run=False,
        number_index=_lander_number_index(),
    )
    assert len(upgrades) == 3

    create_calls = client.create_seller_listing.call_args_list
    by_scryfall = {c.kwargs["scryfall_id"]: c.kwargs["quantity"] for c in create_calls}
    # Lander #7's two contributing DFTs (qty 2 + 1) consolidated into one create call.
    assert by_scryfall["lander-7"] == 3
    # Lander #8 stayed separate, with only its own quantity.
    assert by_scryfall["lander-8"] == 3
    assert len(create_calls) == 2


def test_double_sided_upgrade_malformed_number_falls_back_to_name_index():
    """A listing with no usable compound number (blank, or not exactly two parts)
    falls back to the old name-based lookup rather than silently finding nothing."""
    listing = _seller_listing(
        "Human Soldier // Lander", "TEOE", scryfall_id="human-soldier-2",
        inventory_id="inv-1", number="",  # no number data at all
        quantity=1, price_usd=0.15,
    )
    client = MagicMock()
    upgrades = apply_double_sided_upgrades(
        [listing], _lander_name_index_bug(), {}, PricingConfig(), client, dry_run=True,
        number_index=_lander_number_index(),
    )
    assert len(upgrades) == 1
    # Falls back to whatever build_name_index() picked -- not a crash, not silently dropped.
    assert upgrades[0].upgrade_scryfall_id == "lander-8"
