from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from manabot.models import MatchResult, TrendDirection

_TREND_TEXT = {
    TrendDirection.UP: "UP",
    TrendDirection.DOWN: "DOWN",
    TrendDirection.FLAT: "FLAT",
    TrendDirection.NEW: "NEW",
}

COLUMNS = [
    "card_name", "scryfall_id", "tags", "best_price_usd", "max_price_usd",
    "quantity_available", "min_condition", "foil", "trend", "change_pct",
    "is_good_buy", "status",
]


def write(results: list[MatchResult], reports_dir: Path, run_at: datetime) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"report_{run_at.strftime('%Y%m%d_%H%M%S')}.csv"

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in results:
            item = r.buy_list_item
            trend_dir = _TREND_TEXT.get(r.trend.direction) if r.trend else ""
            change_pct = f"{r.trend.change_pct:.1f}" if r.trend and r.trend.change_pct is not None else ""
            writer.writerow({
                "card_name": item.card_name,
                "scryfall_id": item.scryfall_id or "",
                "tags": "|".join(item.tags),
                "best_price_usd": f"{r.best_price:.2f}" if r.best_price is not None else "",
                "max_price_usd": f"{item.max_price_usd:.2f}",
                "quantity_available": r.listings[0].quantity_available if r.listings else "",
                "min_condition": item.min_condition.value,
                "foil": item.foil.value,
                "trend": trend_dir,
                "change_pct": change_pct,
                "is_good_buy": "yes" if r.is_good_buy else "no",
                "status": r.status.value,
            })

    return path
