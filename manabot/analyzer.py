from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from manabot.db import get_price_history
from manabot.models import MatchResult, MatchStatus, TrendData, TrendDirection

log = logging.getLogger(__name__)


def analyze(
    results: list[MatchResult],
    conn: sqlite3.Connection,
    trend_window_days: int = 7,
    trend_threshold_pct: float = 5.0,
) -> list[MatchResult]:
    """Enrich each MatchResult with trend data from price history."""
    for result in results:
        if result.status == MatchStatus.UNRESOLVED or not result.listings:
            continue

        scryfall_id = result.buy_list_item.scryfall_id or (
            result.listings[0].scryfall_id if result.listings else None
        )
        if not scryfall_id:
            continue

        result.trend = _compute_trend(
            conn, scryfall_id, result.best_price or 0.0, trend_window_days, trend_threshold_pct
        )

    return results


def _compute_trend(
    conn: sqlite3.Connection,
    scryfall_id: str,
    price_now: float,
    window_days: int,
    threshold_pct: float,
) -> TrendData:
    history = get_price_history(conn, scryfall_id, days=window_days)

    if not history:
        return TrendData(
            scryfall_id=scryfall_id,
            price_now=price_now,
            price_then=None,
            direction=TrendDirection.NEW,
        )

    # Use the oldest price in the window as the reference point
    price_then = history[0][1]
    trend = TrendData(
        scryfall_id=scryfall_id,
        price_now=price_now,
        price_then=price_then,
        direction=TrendDirection.FLAT,
    )

    change = trend.change_pct
    if change is None:
        trend.direction = TrendDirection.FLAT
    elif change > threshold_pct:
        trend.direction = TrendDirection.UP
    elif change < -threshold_pct:
        trend.direction = TrendDirection.DOWN
    else:
        trend.direction = TrendDirection.FLAT

    return trend


def summarize(results: list[MatchResult]) -> dict:
    total = len(results)
    good_buys = sum(1 for r in results if r.is_good_buy)
    unresolved = sum(1 for r in results if r.status == MatchStatus.UNRESOLVED)
    warn_scryfall = sum(1 for r in results if r.status == MatchStatus.WARN_SCRYFALL_NEEDED)
    return {
        "total_checked": total,
        "good_buy_count": good_buys,
        "unresolved_count": unresolved,
        "warn_scryfall_count": warn_scryfall,
    }
