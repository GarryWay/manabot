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
    scryfall_ids: Optional[set] = None,
    name_set_filter: Optional[set] = None,
) -> list[dict]:
    """Return parsed catalog records, using cache when fresh enough.

    Pass scryfall_ids to include records by scryfall_id (inventory items).
    Pass name_set_filter as a set of (set_code, name) tuples to also include
    records that match by name within a set — used to pull in same-set single-sided
    counterparts of double-sided token listings without knowing their scryfall_ids.
    Both filters are OR'd together.
    """
    cache_path = Path(cache_path)
    if not cache_path.exists():
        _download_catalog(cache_path, url)
    else:
        age_hours = (datetime.now(timezone.utc).timestamp() - cache_path.stat().st_mtime) / 3600
        if age_hours >= max_age_hours:
            log.info("Catalog cache is %.1fh old, downloading fresh copy...", age_hours)
            _download_catalog(cache_path, url)
        else:
            log.info("Loading catalog from cache (%s, %.1fh old)", cache_path, age_hours)

    return _parse_catalog(cache_path, scryfall_ids, name_set_filter)


def _download_catalog(cache_path: Path, url: str) -> None:
    log.info("Downloading catalog from %s ...", url)
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = resp.read()
    log.info("Downloaded %.1f MB compressed", len(raw) / 1_048_576)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(raw)
    log.info("Cached to %s", cache_path)


def _parse_catalog(
    cache_path: Path,
    scryfall_ids: Optional[set],
    name_set_filter: Optional[set] = None,
) -> list[dict]:
    """Stream-parse the gzipped catalog JSON, keeping records that match either filter."""
    def _keep(r: dict) -> bool:
        if scryfall_ids is None and name_set_filter is None:
            return True
        if scryfall_ids and r.get("scryfall_id") in scryfall_ids:
            return True
        if name_set_filter:
            key = (str(r.get("set_code", "")).upper(), r.get("name", ""))
            if key in name_set_filter:
                return True
        return False

    try:
        import ijson  # type: ignore[import-untyped]
        with gzip.open(cache_path, "rb") as f:
            records = [r for r in ijson.items(f, "data.item") if _keep(r)]
        log.info(
            "Catalog loaded: %d record(s)%s",
            len(records),
            f" (filtered to {len(scryfall_ids or [])} inventory IDs"
            + (f" + {len(name_set_filter)} name lookups)" if name_set_filter else ")")
            if (scryfall_ids or name_set_filter) else "",
        )
        return records
    except ImportError:
        log.warning(
            "ijson not installed — loading full catalog into memory (high RAM usage); "
            "run: pip install ijson"
        )
        with gzip.open(cache_path) as f:
            all_records: list[dict] = json.load(f)["data"]
        if scryfall_ids is not None or name_set_filter is not None:
            records = [r for r in all_records if _keep(r)]
            del all_records
            return records
        return all_records


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


def build_name_index(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Index records by (set_code, name) for same-set name lookups.

    When multiple records share the same set+name (shouldn't happen but defensive),
    the one with the highest market price is kept.
    """
    index: dict[tuple[str, str], dict] = {}
    for record in records:
        key = (str(record.get("set_code", "")).upper(), record.get("name", ""))
        existing = index.get(key)
        if existing is None:
            index[key] = record
        else:
            # Keep the record with the higher market price
            new_mkt = record.get("price_market") or 0
            old_mkt = existing.get("price_market") or 0
            if new_mkt > old_mkt:
                index[key] = record
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
