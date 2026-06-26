"""ManaPool catalog bulk download and local caching.

Publicly available daily snapshot (no auth required):
  https://storage.googleapis.com/manapool-prod-catalog/singles.json.gz

Use load_catalog() to get the data (downloads if cache is stale), then
build_variant_index() to get an O(1) lookup by (scryfall_id, condition_id, finish_id, language_id).
"""
from __future__ import annotations

import gzip
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CATALOG_URL = "https://storage.googleapis.com/manapool-prod-catalog/singles.json.gz"

# Type alias for the variant index key
VariantKey = tuple[str, str, str, str]  # (scryfall_id, condition_id, finish_id, language_id)


@dataclass
class CatalogVariant:
    scryfall_id: str
    card_name: str
    set_code: str
    condition_id: str           # "NM", "LP", "MP", "HP", "DMG"
    finish_id: str              # "NF", "FO", "EF"
    language_id: str            # "EN", "JA", etc.
    low_price_usd: float        # lowest current listing price; 0.0 if none
    available_quantity: int
    recent_sales: list[dict]    # [{created_at: str, price: int (cents), quantity: int}]
    market_price_usd: Optional[float]   # from parent record (foil-aware)


def load_catalog(
    cache_path: Path,
    max_age_hours: float = 23.0,
    url: str = CATALOG_URL,
) -> list[dict]:
    """Return parsed catalog records, using cache when fresh enough.

    Downloads a fresh copy if the cache doesn't exist or is older than max_age_hours.
    Returns the list from data['data'].
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        age_hours = (datetime.now(timezone.utc).timestamp() - cache_path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            log.info("Loading catalog from cache (%s, %.1fh old)", cache_path, age_hours)
            with gzip.open(cache_path) as f:
                return json.load(f)["data"]

    log.info("Downloading catalog from %s ...", url)
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = resp.read()
    log.info("Downloaded %.1f MB compressed", len(raw) / 1_048_576)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(raw)
    log.info("Cached to %s", cache_path)

    with gzip.open(cache_path) as f:
        return json.load(f)["data"]


def build_variant_index(records: list[dict]) -> dict[VariantKey, CatalogVariant]:
    """Index all variants by (scryfall_id, condition_id, finish_id, language_id)."""
    index: dict[VariantKey, CatalogVariant] = {}
    for record in records:
        scryfall_id = record.get("scryfall_id", "")
        card_name = record.get("name", "")
        set_code = str(record.get("set_code", "")).upper()
        market_nf = _cents_to_usd(record.get("price_market"))
        market_fo = _cents_to_usd(record.get("price_market_foil"))

        for v in record.get("variants", []):
            cond = v.get("condition_id", "")
            finish = v.get("finish_id", "")
            lang = v.get("language_id", "EN")
            market = market_fo if finish in ("FO", "EF") else market_nf
            key: VariantKey = (scryfall_id, cond, finish, lang)
            index[key] = CatalogVariant(
                scryfall_id=scryfall_id,
                card_name=card_name,
                set_code=set_code,
                condition_id=cond,
                finish_id=finish,
                language_id=lang,
                low_price_usd=_cents_to_usd(v.get("low_price")) or 0.0,
                available_quantity=int(v.get("available_quantity", 0)),
                recent_sales=v.get("recent_sales") or [],
                market_price_usd=market,
            )
    return index


def get_liquidity_score(recent_sales: list[dict], lookback_days: int = 60) -> float:
    """Return average quantity sold per 30 days over the lookback window.

    Returns 0.0 if no sales within the window.
    """
    if not recent_sales or lookback_days <= 0:
        return 0.0
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
    qty = 0
    for sale in recent_sales:
        try:
            ts = datetime.fromisoformat(sale["created_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            qty += int(sale.get("quantity", 1))
    return (qty / lookback_days) * 30  # normalise to per-30-days


def _cents_to_usd(value: Any) -> Optional[float]:
    try:
        cents = int(value)
        return cents / 100.0 if cents > 0 else None
    except (TypeError, ValueError):
        return None
