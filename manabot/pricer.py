"""Seller inventory pricing engine.

Pricing algorithm per listing
------------------------------
1. Fetch the CatalogVariant matching (scryfall_id, condition, finish, language).
2. Run linear regression on recent_sales (price vs days-since-oldest-sale) to
   project the expected price at today's position on the trend line.
3. Compare trend projection to low_price (lowest current listing):
     - low_price ≈ projection (within race_to_bottom_threshold): price at low_price - $0.01
     - low_price << projection (race to bottom):                  price at projection (don't chase)
     - no low_price (no active listings):                         price at projection
     - no recent_sales, but low_price exists:                     price at low_price - $0.01
     - neither:                                                    leave price unchanged (no_data)
4. This computed price is stored as trend_target_usd — unmodified for reporting.
5. Apply cost floor: if cost_basis known and days_below_floor < cost_floor_days:
       new_price = max(trend_target, cost_basis × (1 + min_margin_pct))
6. Apply hard floor: new_price = max(new_price, $0.15)
7. Update only if |new_price - current_price| >= $0.01.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from manabot.api.manapool_catalog import CatalogVariant
from manabot.models import Condition, Finish

if TYPE_CHECKING:
    from manabot.api.manapool import ManaPoolClient
    from manabot.config import Config

log = logging.getLogger(__name__)

HARD_FLOOR_USD: float = 0.15

_FINISH_TO_CATALOG_ID: dict[Finish, str] = {
    Finish.NONFOIL: "NF",
    Finish.FOIL: "FO",
    Finish.ANY: "NF",
}


@dataclass
class PricingConfig:
    race_to_bottom_threshold: float = 0.20  # low_price < projection × (1 - this) = race to bottom
    min_margin_pct: float = 0.10            # cost floor = cost_basis × (1 + this)
    cost_floor_days: int = 30               # days below floor before constraint lifts
    liquidity_lookback_days: int = 60       # window for sales when computing liquidity
    max_sale_age_days: int = 90             # ignore sales data if most recent sale is older than this
    min_sales_for_regression: int = 3       # minimum sales needed to run regression
    iqr_fence_factor: float = 1.5           # Tukey fence multiplier for outlier removal (1.5 = standard, 3.0 = extreme only)
    finish_merge_max_price_usd: float = 2.0  # pool foil+nonfoil sales only when market price is below this
    finish_merge_threshold_usd: float = 1.0  # pool foil+nonfoil sales only when their prices are within this


@dataclass
class PriceRecommendation:
    scryfall_id: str
    card_name: str
    set_code: str
    condition: Condition
    finish: Finish
    language: str
    current_price_usd: float
    trend_target_usd: Optional[float]   # raw market signal — no floors applied
    new_price_usd: float                # final price after cost floor + hard floor
    market_price_usd: Optional[float]
    low_price_usd: Optional[float]
    tcg_market_usd: Optional[float]     # TCGPlayer market price (from TCGTracking)
    cost_basis_usd: Optional[float]
    reason: str   # 'trend_beat_low', 'trend_race_to_bottom', 'trend_no_listings',
                  # 'no_sales_beat_low', 'tcg_market', 'no_data', 'cost_floor', 'hard_floor'
    should_update: bool


# ---------------------------------------------------------------------------
# Linear regression (stdlib only)
# ---------------------------------------------------------------------------

def _linear_regression(x_vals: list[float], y_vals: list[float]) -> tuple[float, float]:
    """Return (slope, intercept) for the least-squares line through the points."""
    n = len(x_vals)
    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)
    if denominator == 0.0:
        return 0.0, y_mean
    slope = numerator / denominator
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _iqr_filter(
    points: list[tuple[datetime, float]],
    fence_factor: float = 1.5,
) -> list[tuple[datetime, float]]:
    """Remove price outliers using Tukey's IQR fence method.

    Adapts to the actual distribution rather than a fixed ratio of the median,
    so genuinely declining cards aren't penalised for having historically high
    prices that now look like outliers relative to a high median.
    """
    prices = sorted(p for _, p in points)
    n = len(prices)
    if n < 4:
        return points
    q1 = statistics.median(prices[: n // 2])
    q3 = statistics.median(prices[(n + 1) // 2 :])
    iqr = q3 - q1
    if iqr == 0:
        return points  # all the same price — nothing to filter
    lo = q1 - fence_factor * iqr
    hi = q3 + fence_factor * iqr
    return [(dt, p) for dt, p in points if lo <= p <= hi]


def _project_price(
    recent_sales: list[dict],
    max_sale_age_days: int = 90,
    min_sales: int = 3,
    iqr_fence_factor: float = 1.5,
) -> Optional[float]:
    """Project price at the last-sale date using linear regression over recent_sales.

    Guards against unreliable extrapolation:
    - Requires at least min_sales data points.
    - Ignores data when the most recent sale is older than max_sale_age_days.
    - Removes outliers via Tukey IQR fences before fitting, so anomalous
      transactions don't dominate the slope.
    - Projects to the last *remaining* sale date, never beyond the data range.
    """
    sales = sorted(recent_sales, key=lambda s: s.get("created_at", ""))
    if len(sales) < min_sales:
        return None

    now = datetime.now(timezone.utc)
    try:
        last_dt = datetime.fromisoformat(sales[-1]["created_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return None

    if (now - last_dt).days > max_sale_age_days:
        return None

    points: list[tuple[datetime, float]] = []
    for sale in sales:
        try:
            dt = datetime.fromisoformat(sale["created_at"].replace("Z", "+00:00"))
            price = int(sale["price"]) / 100.0
            points.append((dt, price))
        except (ValueError, KeyError):
            continue

    if len(points) < min_sales:
        return None

    points = _iqr_filter(points, fence_factor=iqr_fence_factor)

    if len(points) < 2:
        return None

    first_dt = min(dt for dt, _ in points)
    last_remaining_dt = max(dt for dt, _ in points)
    x_vals = [(dt - first_dt).total_seconds() / 86400.0 for dt, _ in points]
    y_vals = [p for _, p in points]

    slope, intercept = _linear_regression(x_vals, y_vals)

    x_project = (last_remaining_dt - first_dt).total_seconds() / 86400.0
    return max(intercept + slope * x_project, 0.01)


def _recent_sale_price(recent_sales: list[dict], max_sale_age_days: int) -> Optional[float]:
    """Return the price of the most recent sale if it falls within max_sale_age_days, else None."""
    if not recent_sales:
        return None
    sales = sorted(recent_sales, key=lambda s: s.get("created_at", ""))
    last = sales[-1]
    try:
        last_dt = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return None
    if (datetime.now(timezone.utc) - last_dt).days > max_sale_age_days:
        return None
    return int(last["price"]) / 100.0


def _compute_trend_target(
    variant: CatalogVariant,
    config: PricingConfig,
    tcg_market_usd: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """Compute the unflored trend target and reason string.

    When ManaPool shows no competing listings (low_price == 0), TCGPlayer market
    price is used as the reference — either alone or to cap an unreliable
    regression when both signals are available.
    """
    low = variant.low_price_usd if variant.low_price_usd > 0 else None
    projected = _project_price(
        variant.recent_sales,
        max_sale_age_days=config.max_sale_age_days,
        min_sales=config.min_sales_for_regression,
        iqr_fence_factor=config.iqr_fence_factor,
    )

    if projected is not None:
        if low is not None:
            if low < projected * (1.0 - config.race_to_bottom_threshold):
                return projected, "trend_race_to_bottom"
            return max(low - 0.01, 0.01), "trend_beat_low"
        # No competing ManaPool listings — TCGPlayer market is a better signal than
        # a sparse-data projection, so prefer it when available.
        if tcg_market_usd is not None:
            return tcg_market_usd, "tcg_market"
        return projected, "trend_no_listings"

    # Not enough sales for regression — try single-sale fallback (recency-gated).
    # One transaction is not enough confidence to hold above the market low, so the
    # race-to-bottom guard is intentionally skipped here: always follow the current low.
    single_price = _recent_sale_price(variant.recent_sales, config.max_sale_age_days)
    if single_price is not None:
        if low is not None:
            return max(low - 0.01, 0.01), "trend_beat_low"
        if tcg_market_usd is not None:
            return tcg_market_usd, "tcg_market"
        return single_price, "trend_no_listings"

    if low is not None:
        return max(low - 0.01, 0.01), "no_sales_beat_low"
    # No ManaPool data at all — fall back to TCGPlayer market if available
    if tcg_market_usd is not None:
        return tcg_market_usd, "tcg_market"
    return None, "no_data"


def compute_price(
    listing_scryfall_id: str,
    listing_card_name: str,
    listing_set_code: str,
    listing_condition: Condition,
    listing_finish: Finish,
    listing_language: str,
    listing_current_price_usd: float,
    catalog_variant: Optional[CatalogVariant],
    cost_basis_usd: Optional[float],
    days_below_floor: int,
    config: PricingConfig,
    tcg_market_usd: Optional[float] = None,
) -> PriceRecommendation:
    """Compute the optimal price for one of our seller listings."""
    def _make_rec(
        trend_target: Optional[float],
        new_price: float,
        market: Optional[float],
        low: Optional[float],
        reason: str,
        update: bool,
    ) -> PriceRecommendation:
        return PriceRecommendation(
            scryfall_id=listing_scryfall_id,
            card_name=listing_card_name,
            set_code=listing_set_code,
            condition=listing_condition,
            finish=listing_finish,
            language=listing_language,
            current_price_usd=listing_current_price_usd,
            trend_target_usd=round(trend_target, 2) if trend_target is not None else None,
            new_price_usd=new_price,
            market_price_usd=market,
            low_price_usd=low,
            tcg_market_usd=round(tcg_market_usd, 2) if tcg_market_usd is not None else None,
            cost_basis_usd=cost_basis_usd,
            reason=reason,
            should_update=update,
        )

    if catalog_variant is None:
        if tcg_market_usd is not None:
            # No ManaPool catalog entry — price from TCGPlayer market with floors
            trend_target = tcg_market_usd
            new_price = trend_target
            reason = "tcg_market"
            if cost_basis_usd is not None:
                floor = cost_basis_usd * (1.0 + config.min_margin_pct)
                if days_below_floor < config.cost_floor_days and new_price < floor:
                    new_price = floor
                    reason = "cost_floor"
            if new_price < HARD_FLOOR_USD:
                new_price = HARD_FLOOR_USD
                reason = "hard_floor"
            new_price = round(new_price, 2)
            return _make_rec(trend_target, new_price, None, None, reason, abs(new_price - listing_current_price_usd) >= 0.005)
        return _make_rec(None, listing_current_price_usd, None, None, "no_data", False)

    trend_target, reason = _compute_trend_target(catalog_variant, config, tcg_market_usd)

    if trend_target is None:
        return _make_rec(
            None,
            listing_current_price_usd,
            catalog_variant.market_price_usd,
            catalog_variant.low_price_usd if catalog_variant.low_price_usd > 0 else None,
            "no_data",
            False,
        )

    # Apply floors to get the actual price to set (trend_target stays unmodified)
    new_price = trend_target
    if cost_basis_usd is not None:
        floor_price = cost_basis_usd * (1.0 + config.min_margin_pct)
        if days_below_floor < config.cost_floor_days and new_price < floor_price:
            new_price = floor_price
            reason = "cost_floor"

    if new_price < HARD_FLOOR_USD:
        new_price = HARD_FLOOR_USD
        reason = "hard_floor"

    new_price = round(new_price, 2)

    return _make_rec(
        trend_target,
        new_price,
        catalog_variant.market_price_usd,
        catalog_variant.low_price_usd if catalog_variant.low_price_usd > 0 else None,
        reason,
        abs(new_price - listing_current_price_usd) >= 0.005,
    )


def run_pricing_update(
    client: "ManaPoolClient",
    conn: sqlite3.Connection,
    config: "Config",
    pricing_config: Optional[PricingConfig] = None,
    dry_run: bool = False,
) -> list[PriceRecommendation]:
    """Load catalog, fetch seller inventory, compute + apply price updates."""
    from manabot.api.manapool_catalog import build_variant_index, load_catalog
    from manabot.api.tcgtracking import TCGTrackingClient
    from manabot.db import (
        get_cost_basis, get_days_below_floor, get_last_sales_sync,
        log_price_update, record_sales, update_floor_tracking,
    )

    if pricing_config is None:
        pricing_config = PricingConfig(
            race_to_bottom_threshold=getattr(config, "pricer_race_to_bottom_threshold", 0.20),
            min_margin_pct=getattr(config, "pricer_min_margin_pct", 0.10),
            cost_floor_days=getattr(config, "pricer_cost_floor_days", 30),
        )

    tcg_cache_dir = Path(getattr(config, "tcg_cache_dir", "data/tcgtracking"))
    tcg = TCGTrackingClient(cache_dir=tcg_cache_dir)

    log.info("Fetching seller inventory...")
    our_inventory = client.get_seller_inventory()
    if not our_inventory:
        log.info("No seller inventory found")
        return []
    log.info("Found %d seller listing(s)", len(our_inventory))

    inventory_ids = {listing.scryfall_id for listing in our_inventory}
    catalog_cache = Path(getattr(config, "catalog_cache_path", "data/manapool_catalog.json.gz"))
    log.info("Loading catalog (filtering to %d inventory IDs)...", len(inventory_ids))
    records = load_catalog(catalog_cache, scryfall_ids=inventory_ids)
    variant_index = build_variant_index(records)
    log.info("Catalog indexed: %d variants", len(variant_index))

    since = get_last_sales_sync(conn)
    try:
        sales = client.get_completed_sales(since=since)
        if sales:
            n = record_sales(conn, sales)
            log.info("Recorded %d new completed sale(s)", n)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not sync sales history: %s", e)

    recommendations: list[PriceRecommendation] = []
    n_would_update = 0

    for listing in our_inventory:
        finish_catalog_id = _FINISH_TO_CATALOG_ID.get(listing.finish, "NF")
        key = (listing.scryfall_id, listing.condition.value, finish_catalog_id, listing.language)
        catalog_variant = variant_index.get(key)

        # Pool foil + nonfoil sales when data is sparse and finishes are priced similarly.
        # Applies to NF/FO only (not etched) when the market price of either finish is
        # below finish_merge_max_price_usd and their prices are within finish_merge_threshold_usd.
        if (
            catalog_variant is not None
            and finish_catalog_id in ("NF", "FO")
            and len(catalog_variant.recent_sales) < pricing_config.min_sales_for_regression
        ):
            other_finish_id = "NF" if finish_catalog_id == "FO" else "FO"
            other_key = (listing.scryfall_id, listing.condition.value, other_finish_id, listing.language)
            other_variant = variant_index.get(other_key)
            if other_variant is not None and other_variant.recent_sales:
                our_price = catalog_variant.market_price_usd or catalog_variant.low_price_usd or 0.0
                other_price = other_variant.market_price_usd or other_variant.low_price_usd or 0.0
                prices_close = (
                    our_price == 0.0
                    or other_price == 0.0
                    or abs(our_price - other_price) <= pricing_config.finish_merge_threshold_usd
                )
                price_is_low = (
                    0 < our_price < pricing_config.finish_merge_max_price_usd
                    or 0 < other_price < pricing_config.finish_merge_max_price_usd
                )
                if prices_close and price_is_low:
                    from dataclasses import replace as dc_replace
                    merged = list(catalog_variant.recent_sales) + list(other_variant.recent_sales)
                    catalog_variant = dc_replace(catalog_variant, recent_sales=merged)

        cost_row = get_cost_basis(conn, listing.scryfall_id, listing.condition, listing.finish)
        cost_basis_usd = cost_row["cost_usd"] if cost_row else None
        days_below = get_days_below_floor(conn, listing.scryfall_id, listing.condition, listing.finish)

        tcg_market = tcg.get_market_price(
            listing.scryfall_id,
            listing.set_code,
            listing.condition.value,
            listing.finish.value,
        )
        # Finish fallback: if no TCGPlayer data for this finish and it's a cheap NF/FO card,
        # try the other finish as a proxy (mirrors the ManaPool catalog finish-merge logic).
        if tcg_market is None and listing.finish.value in ("nonfoil", "foil"):
            other_tcg_finish = "foil" if listing.finish.value == "nonfoil" else "nonfoil"
            tcg_market = tcg.get_market_price(
                listing.scryfall_id,
                listing.set_code,
                listing.condition.value,
                other_tcg_finish,
            )
            if tcg_market is not None and tcg_market >= pricing_config.finish_merge_max_price_usd:
                tcg_market = None  # other finish is expensive — not a valid proxy

        rec = compute_price(
            listing_scryfall_id=listing.scryfall_id,
            listing_card_name=listing.card_name,
            listing_set_code=listing.set_code,
            listing_condition=listing.condition,
            listing_finish=listing.finish,
            listing_language=listing.language,
            listing_current_price_usd=listing.price_usd,
            catalog_variant=catalog_variant,
            cost_basis_usd=cost_basis_usd,
            days_below_floor=days_below,
            config=pricing_config,
            tcg_market_usd=tcg_market,
        )
        recommendations.append(rec)

        cost_floor = (cost_basis_usd * (1.0 + pricing_config.min_margin_pct)) if cost_basis_usd else None
        update_floor_tracking(conn, listing.scryfall_id, listing.condition, listing.finish, rec.new_price_usd, cost_floor)

        if rec.should_update:
            n_would_update += 1
            log_price_update(
                conn,
                scryfall_id=rec.scryfall_id,
                card_name=rec.card_name,
                set_code=rec.set_code,
                condition=rec.condition,
                finish=rec.finish,
                old_price_usd=rec.current_price_usd,
                new_price_usd=rec.new_price_usd,
                market_price_usd=rec.market_price_usd,
                list_floor_usd=rec.low_price_usd,
                reason=rec.reason,
                dry_run=dry_run,
            )
            if not dry_run:
                try:
                    client.update_seller_listing_price(
                        listing.scryfall_id, listing.condition, listing.finish,
                        rec.new_price_usd, listing.quantity, listing.language,
                    )
                    log.info(
                        "Updated %-40s [%s] %s/%-7s  $%.2f → $%.2f (trend=$%.2f, %s)",
                        rec.card_name[:40], rec.set_code,
                        rec.condition.value, rec.finish.value,
                        rec.current_price_usd, rec.new_price_usd,
                        rec.trend_target_usd or 0.0, rec.reason,
                    )
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to update %r: %s", rec.card_name, e)
            else:
                log.info(
                    "[dry-run] %-40s [%s] %s/%-7s  $%.2f → $%.2f (trend=$%.2f, %s)",
                    rec.card_name[:40], rec.set_code,
                    rec.condition.value, rec.finish.value,
                    rec.current_price_usd, rec.new_price_usd,
                    rec.trend_target_usd or 0.0, rec.reason,
                )

    skipped = sum(1 for r in recommendations if r.reason == "no_data")
    log.info(
        "Pricing run: %d listing(s), %d to update, %d skipped (no data)",
        len(recommendations), n_would_update, skipped,
    )
    return recommendations
