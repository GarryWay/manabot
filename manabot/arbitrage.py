"""Arbitrage scanner — find ManaPool listings trading below market value.

Workflow
--------
1. Fetch all singles prices from ManaPool.
2. Pass 1 — build cheapest-NM index: for each card name, find the cheapest NM
   nonfoil listing across ALL printings. That price is the "true market floor";
   buyers gravitate to the cheapest version, so it's the reference price.
3. Pass 2 — find the cheapest LP listing for each card, across ALL printings.
   Compare it to the cheapest-NM floor. Cards with no live NM listing are skipped.
4. A card is a candidate when:
     cheapest_lp_price < nm_floor_price × (1 − min_discount_pct / 100)
   and available_quantity >= min_quantity (liquidity proxy).
5. Build a BuyListItem with max_price = nm_floor_price, no set_code constraint.
   The optimizer is free to find any printing — if it finds a copy cheaper than
   our LP scan price, that only improves the margin.
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

# Import lazily to avoid hard dependency when scryfall_bulk isn't available
try:
    from manabot.api.scryfall_bulk import ScryfallBulk
except ImportError:
    ScryfallBulk = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# NM listings set the market reference; LP listings are the purchase targets.
_RESALE_CONDITIONS = {Condition.LP}


@dataclass
class ArbitrageCandidate:
    listing: PriceListing        # cheapest LP listing found (any printing)
    market_price_usd: float      # cheapest live NM price across all printings
    discount_pct: float          # how far below market (positive = below)
    buy_list_item: BuyListItem   # ready to pass to the optimizer (no printing constraint)


def find_candidates(
    listings: list[PriceListing],
    scryfall: "ScryfallBulk | None" = None,
    min_discount_pct: float = 10.0,
    min_quantity: int = 20,
    min_market_price_usd: float = 2.00,
    target_quantity: int = 1,
    min_set_age_days: int = 30,
) -> list[ArbitrageCandidate]:
    """Scan all listings and return arbitrage candidates sorted by discount depth.

    Args:
        listings:              All fetched PriceListing objects.
        scryfall:              ScryfallBulk instance for sanctioned-card filtering.
        min_discount_pct:      Minimum % below NM floor price to qualify (e.g. 10.0).
        min_quantity:          Minimum available_quantity as a liquidity proxy (default 20).
        min_market_price_usd:  Minimum NM floor price; excludes bulk commons (default $2.00).
        target_quantity:       Quantity to put in each generated BuyListItem (default 1).

    Returns:
        List of ArbitrageCandidate sorted by discount_pct descending (best deal first).
    """
    # Pass 1: cheapest NM nonfoil price per card name, across ALL printings.
    cheapest_nm: dict[str, float] = {}
    for listing in listings:
        if listing.condition != Condition.NM or listing.finish != Finish.NONFOIL:
            continue
        name = listing.card_name
        current = cheapest_nm.get(name)
        if current is None or listing.price_usd < current:
            cheapest_nm[name] = listing.price_usd

    # Pass 2: every LP listing below the NM floor, one candidate per (card, printing).
    # min_quantity is a pass/fail liquidity gate only — not proportional to purchase size.
    candidates: list[ArbitrageCandidate] = []
    skipped_unsanctioned = 0
    skipped_tokens = 0
    skipped_new_sets = 0

    for listing in listings:
        if listing.finish != Finish.NONFOIL:
            continue
        if listing.condition not in _RESALE_CONDITIONS:
            continue

        nm_floor = cheapest_nm.get(listing.card_name)
        if nm_floor is None:
            continue  # no live NM reference — skip

        if nm_floor < min_market_price_usd:
            continue
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
        if listing.price_usd >= nm_floor:
            continue

        discount_pct = (1.0 - listing.price_usd / nm_floor) * 100.0
        if discount_pct < min_discount_pct - 1e-9:  # inclusive of exact threshold
            continue

        item = BuyListItem(
            card_name=listing.card_name,
            scryfall_id=listing.scryfall_id,
            target_quantity=target_quantity,
            max_price_usd=round(nm_floor, 2),  # cheapest NM = what it's worth
            min_condition=Condition.LP,
            foil=Finish.NONFOIL,
            # No allowed_sets constraint — optimizer finds cheapest printing available.
            # If it finds a copy cheaper than our LP scan price, margin only improves.
        )
        candidates.append(ArbitrageCandidate(
            listing=listing,
            market_price_usd=nm_floor,
            discount_pct=discount_pct,
            buy_list_item=item,
        ))

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
