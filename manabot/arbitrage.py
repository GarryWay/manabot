"""Arbitrage scanner — find ManaPool listings trading below market value.

Workflow
--------
1. Fetch all singles prices from ManaPool.
2. Pass 1 — card-level market floor: for each card name, find the minimum
   market_price_usd across ALL listings (all printings, all finishes, all
   conditions). ManaPool's market_price_usd is derived from transaction history
   and is more reliable than any single listing's asking price.
   - Cards where ANY version's market price < min_market_price_usd are fully
     ineligible — a $0.15 foil reprint marks the card as bulk even if older
     printings haven't repriced yet.
   - Listings with market_price_usd=None are excluded from the floor: no
     transaction history means no reliable signal even with many listings.
3. Pass 2 — scan LP+ (LP or NM) listings in any finish. An eligible card's
   listing priced below the card-level market floor is an arbitrage candidate.
   Using the card-level floor prevents inflated old-printing prices from
   generating false discount signals when a cheap reprint exists.
   The per-listing market_price_usd must be non-None to confirm the specific
   version has enough sales volume to be worth buying.
4. A listing is a candidate when:
     listing_price < card_min_market × (1 − min_discount_pct / 100)
   and card_min_market >= min_market_price_usd (excludes bulk)
   and available_quantity >= min_quantity (liquidity proxy).
5. Build a BuyListItem per candidate with max_price = card_min_market and
   foil = listing.finish (match the candidate's finish type). No set_code
   constraint — the optimizer finds the cheapest qualifying copy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from manabot.models import (
    BuyListItem,
    Condition,
    Finish,
    PriceListing,
)

try:
    from manabot.api.scryfall_bulk import ScryfallBulk
except ImportError:
    ScryfallBulk = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# LP or better (LP, NM) in any finish are valid purchase targets.
# The card-level minimum market floor (not listing price) is the discount reference,
# so NM listings can be candidates when priced below that floor by other sellers.
_PURCHASE_CONDITIONS = {Condition.NM, Condition.LP}


@dataclass
class ArbitrageCandidate:
    listing: PriceListing        # discounted listing found
    market_price_usd: float      # card-level minimum market price (the discount baseline)
    discount_pct: float          # how far listing_price is below card_min_market (positive = below)
    buy_list_item: BuyListItem   # ready to pass to the optimizer
    liquidity_score: float = 0.0  # avg qty sold per 30 days (0 = unknown / not scored)


def find_candidates(
    listings: list[PriceListing],
    scryfall: "ScryfallBulk | None" = None,
    min_discount_pct: float = 10.0,
    min_quantity: int = 20,
    min_market_price_usd: float = 2.00,
    target_quantity: int = 1,
    min_set_age_days: int = 30,
    catalog_records: list[dict] | None = None,
    min_liquidity_sales: float = 0.0,
    liquidity_lookback_days: int = 60,
) -> list[ArbitrageCandidate]:
    """Scan all listings and return arbitrage candidates sorted by discount depth.

    Args:
        listings:              All fetched PriceListing objects.
        scryfall:              ScryfallBulk instance for sanctioned-card filtering.
        min_discount_pct:      Minimum % below market floor to qualify (e.g. 10.0).
        min_quantity:          Minimum available_quantity as a liquidity proxy (default 20).
        min_market_price_usd:  Minimum market price; excludes bulk cards (default $2.00).
        target_quantity:       Quantity to put in each generated BuyListItem (default 1).
        catalog_records:       Optional ManaPool catalog records for liquidity scoring.
        min_liquidity_sales:   Minimum sales/30d to include a card (0 = no filter).
        liquidity_lookback_days: Days to count sales for liquidity score.

    Returns:
        List of ArbitrageCandidate sorted by discount_pct descending (best deal first).
    """
    # Pass 1: card-level minimum market_price_usd, across ALL printings and ALL finishes.
    # ManaPool's market_price_usd reflects actual transaction history for each version.
    # Taking the card-level minimum ensures a cheap reprint correctly deflates the
    # reference price for older printings that haven't repriced yet.
    # Listings with market_price_usd=None are excluded — no transaction history.
    card_min_market: dict[str, float] = {}
    for listing in listings:
        mp = listing.market_price_usd
        if mp is None:
            continue
        current = card_min_market.get(listing.card_name)
        if current is None or mp < current:
            card_min_market[listing.card_name] = mp

    # Cards where ANY version's market price falls below the threshold are fully
    # ineligible — a cheap foil reprint signals the card has no arbitrage value
    # even if older printings have inflated market prices.
    ineligible_cards: set[str] = {
        name for name, price in card_min_market.items()
        if price < min_market_price_usd
    }

    # Pass 2: LP+ listings (any finish) priced below the card-level market floor.
    candidates: list[ArbitrageCandidate] = []
    skipped_unsanctioned = 0
    skipped_tokens = 0
    skipped_new_sets = 0

    for listing in listings:
        if listing.condition not in _PURCHASE_CONDITIONS:
            continue
        if listing.card_name in ineligible_cards:
            continue

        card_market = card_min_market.get(listing.card_name)
        if card_market is None:
            continue  # card has no market data at all — skip

        if listing.market_price_usd is None:
            continue  # this specific version has insufficient sales volume — skip

        if listing.quantity_available < min_quantity:
            continue
        if scryfall is not None and scryfall.is_token(listing.card_name):
            skipped_tokens += 1
            continue
        if scryfall is not None and not scryfall.is_sanctioned(listing.card_name):
            skipped_unsanctioned += 1
            continue
        if scryfall is not None and scryfall.is_recently_released(listing.set_code, days=min_set_age_days):
            skipped_new_sets += 1
            continue
        if listing.price_usd >= card_market:
            continue

        discount_pct = (1.0 - listing.price_usd / card_market) * 100.0
        if discount_pct < min_discount_pct - 1e-9:
            continue

        item = BuyListItem(
            card_name=listing.card_name,
            scryfall_id=listing.scryfall_id,
            target_quantity=target_quantity,
            max_price_usd=round(card_market, 2),
            min_condition=Condition.LP,
            foil=listing.finish,
        )
        candidates.append(ArbitrageCandidate(
            listing=listing,
            market_price_usd=card_market,
            discount_pct=discount_pct,
            buy_list_item=item,
        ))

    # Liquidity scoring via catalog recent_sales
    if catalog_records is not None:
        from manabot.api.manapool_catalog import build_variant_index, get_liquidity_score
        variant_index = build_variant_index(catalog_records)
        skipped_illiquid = 0
        scored: list[ArbitrageCandidate] = []
        for c in candidates:
            # Use NM/NF/EN as the representative variant for liquidity — most traded
            finish_id = "FO" if c.listing.finish == Finish.FOIL else "NF"
            key = (c.listing.scryfall_id, "NM", finish_id, "EN")
            variant = variant_index.get(key)
            score = get_liquidity_score(
                variant.recent_sales if variant else [],
                lookback_days=liquidity_lookback_days,
            )
            c.liquidity_score = score
            if min_liquidity_sales > 0.0 and score < min_liquidity_sales:
                skipped_illiquid += 1
                continue
            scored.append(c)
        if skipped_illiquid:
            log.info("Filtered out %d illiquid candidate(s) (< %.1f sales/30d)", skipped_illiquid, min_liquidity_sales)
        candidates = scored

    candidates.sort(key=lambda c: c.discount_pct, reverse=True)
    if skipped_tokens:
        log.info("Filtered out %d token card listing(s)", skipped_tokens)
    if skipped_unsanctioned:
        log.info("Filtered out %d non-sanctioned card listing(s)", skipped_unsanctioned)
    if skipped_new_sets:
        log.info("Filtered out %d listing(s) from sets released in the last %d days", skipped_new_sets, min_set_age_days)
    log.info(
        "Arbitrage scan: %d candidates (min discount %.0f%%, min qty %d)",
        len(candidates), min_discount_pct, min_quantity,
    )
    return candidates


def candidates_to_match_results(
    candidates: list[ArbitrageCandidate],
) -> list:
    """Wrap candidates as MatchResult objects so they work with the optimizer."""
    from manabot.models import MatchResult, MatchStatus

    results = []
    for c in candidates:
        results.append(MatchResult(
            buy_list_item=c.buy_list_item,
            listings=[c.listing],
            best_price=c.listing.price_usd,
            is_good_buy=True,
            status=MatchStatus.MATCHED,
        ))
    return results
