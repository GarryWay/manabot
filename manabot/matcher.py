"""
Match fetched price listings against buy list items through a multi-stage filter pipeline:
  1. ID match (scryfall_id)
  2. Name + set filter
  3. Condition filter
  4. Foil/finish filter
  5. In-universe filter (requires Scryfall; degrades gracefully)
"""
from __future__ import annotations

import re
import logging
from typing import Optional

from manabot.models import (
    BuyListItem,
    Condition,
    Finish,
    MatchResult,
    MatchStatus,
    PriceListing,
    _CONDITION_RANK,
)

log = logging.getLogger(__name__)

def match(
    buy_list: list[BuyListItem],
    listings: list[PriceListing],
    scryfall_client=None,
) -> list[MatchResult]:
    """Match all buy list items against the provided listings."""
    by_scryfall_id: dict[str, list[PriceListing]] = {}
    by_name: dict[str, list[PriceListing]] = {}

    for listing in listings:
        if listing.scryfall_id:
            by_scryfall_id.setdefault(listing.scryfall_id, []).append(listing)
        normalized = _normalize_name(listing.card_name)
        by_name.setdefault(normalized, []).append(listing)
        # Also index DFC listings under the front-face name so buylist entries
        # that omit the back-face (e.g. "The Mightstone and Weakstone") still match.
        if " // " in listing.card_name:
            front = _normalize_name(listing.card_name.split(" // ")[0])
            if front != normalized:
                by_name.setdefault(front, []).append(listing)

    results: list[MatchResult] = []
    for item in buy_list:
        results.append(_match_item(item, by_scryfall_id, by_name, scryfall_client))
    return results


def _match_item(
    item: BuyListItem,
    by_scryfall_id: dict[str, list[PriceListing]],
    by_name: dict[str, list[PriceListing]],
    scryfall_client,
) -> MatchResult:
    # Stage 1: ID match
    if item.scryfall_id:
        candidates = by_scryfall_id.get(item.scryfall_id, [])
    else:
        candidates = by_name.get(_normalize_name(item.card_name), [])

    # Stage 2: Set filter
    if item.allowed_sets:
        candidates = [c for c in candidates if c.set_code in item.allowed_sets]

    # Stage 3: Condition filter
    candidates = [c for c in candidates if _condition_qualifies(c.condition, item.min_condition)]

    # Stage 4: Finish filter
    if item.foil != Finish.ANY:
        candidates = [c for c in candidates if c.finish == item.foil]

    # Stage 5: In-universe filter
    warn_scryfall = False
    if item.in_universe_only:
        if scryfall_client is None:
            warn_scryfall = True
            log.warning(
                "%r has in_universe_only=True but no Scryfall client is configured. "
                "Filter skipped — results may include non-in-universe printings.",
                item.card_name,
            )
        else:
            candidates = _filter_in_universe(candidates, scryfall_client)

    if not candidates:
        status = MatchStatus.WARN_SCRYFALL_NEEDED if warn_scryfall else MatchStatus.UNRESOLVED
        return MatchResult(buy_list_item=item, status=status)

    best = min(candidates, key=lambda c: c.price_usd)
    is_good_buy = (
        best.price_usd <= item.max_price_usd
        and best.quantity_available >= item.target_quantity
    )

    status = MatchStatus.WARN_SCRYFALL_NEEDED if warn_scryfall else MatchStatus.MATCHED
    return MatchResult(
        buy_list_item=item,
        listings=candidates,
        best_price=best.price_usd,
        is_good_buy=is_good_buy,
        status=status,
    )


def _condition_qualifies(listing_condition: Condition, min_condition: Condition) -> bool:
    """Return True if listing_condition is at least as good as min_condition."""
    return _CONDITION_RANK[listing_condition] >= _CONDITION_RANK[min_condition]


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _filter_in_universe(
    candidates: list[PriceListing], scryfall_client
) -> list[PriceListing]:
    """Keep only listings where Scryfall confirms the printing is in-universe.

    A listing is excluded when:
    - `flavor_name` is set (alternate universe name printed on the card)
    - `promo_types` contains "universesbeyond" or "sourcematerial"

    If the metadata fetch fails, the listing is included with a warning rather
    than silently dropped.
    """
    filtered = []
    for listing in candidates:
        result = scryfall_client.is_in_universe(listing.scryfall_id)
        if result is None:
            log.warning(
                "Including %r (%s) — Scryfall metadata unavailable, cannot verify printing.",
                listing.card_name, listing.scryfall_id,
            )
            filtered.append(listing)
        elif result:
            filtered.append(listing)
        else:
            log.debug(
                "Excluded non-in-universe printing: %r (%s %s)",
                listing.card_name, listing.set_code, listing.scryfall_id,
            )
    return filtered
