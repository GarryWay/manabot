from __future__ import annotations

import json
import logging
from datetime import datetime

import requests

from manabot.models import MatchResult, TrendDirection

log = logging.getLogger(__name__)

_TREND_ARROW = {
    TrendDirection.UP: "↑",
    TrendDirection.DOWN: "↓",
    TrendDirection.FLAT: "→",
    TrendDirection.NEW: "·",
}


def send(
    results: list[MatchResult],
    webhook_url: str,
    summary: dict,
    run_at: datetime,
    dry_run: bool = False,
) -> None:
    if not webhook_url:
        log.info("Discord webhook not configured — skipping notification.")
        return

    good_buys = [r for r in results if r.is_good_buy]
    payload = _build_payload(good_buys, summary, run_at)

    if dry_run:
        print("[dry-run] Discord payload:")
        print(json.dumps(payload, indent=2))
        return

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Discord notification sent.")
    except requests.RequestException as e:
        log.warning("Discord notification failed: %s", e)


def _build_payload(good_buys: list[MatchResult], summary: dict, run_at: datetime) -> dict:
    count = summary["good_buy_count"]
    color = 0x57F287 if count > 0 else 0x95A5A6  # green or grey

    fields = []
    for r in good_buys[:5]:
        item = r.buy_list_item
        arrow = _TREND_ARROW.get(r.trend.direction, "") if r.trend else ""
        fields.append({
            "name": item.card_name,
            "value": f"${r.best_price:.2f} / max ${item.max_price_usd:.2f} {arrow}",
            "inline": True,
        })

    return {
        "embeds": [
            {
                "title": f"Manabot — {count} good {'buy' if count == 1 else 'buys'} found",
                "color": color,
                "fields": fields,
                "footer": {
                    "text": f"Checked {summary['total_checked']} cards · {run_at.strftime('%Y-%m-%d %H:%M UTC')}"
                },
            }
        ]
    }
