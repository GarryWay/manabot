"""Read-only client for the TCGTracking open API (https://openapi.tcgtracking.com).

No authentication required.  All data is cached to disk with per-endpoint TTLs.
Sets and card product lists are refreshed weekly; SKU pricing daily.

Lookup path:
  set_code → set_id  (from /v1/1/sets, cached weekly)
  scryfall_id → product_id  (from /v1/1/sets/{id}/cards, cached weekly)
  product_id + condition + finish → TCGSKUPricing  (from /v1/1/sets/{id}/skus, cached daily)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://openapi.tcgtracking.com"
_MTG_CAT = 1

_CACHE_SETS_HOURS: float = 168   # 7 days
_CACHE_CARDS_HOURS: float = 168  # 7 days
_CACHE_SKUS_HOURS: float = 23    # ~1 day

# Map our finish values to TCGTracking's "var" field
_FINISH_TO_TCG: dict[str, str] = {
    "foil": "Foil",
    "etched": "Etched",
    "nonfoil": "Normal",
}


@dataclass
class TCGSKUPricing:
    condition: str          # NM / LP / MP / HP / DMG
    finish: str             # Normal / Foil / Etched
    low: float              # TCGPlayer lowest listed price
    market: float           # TCGPlayer market price
    high: float             # TCGPlayer highest listed price
    listing_count: int      # active TCGPlayer listings
    mp_price: Optional[float]  # ManaPool price when available


class TCGTrackingClient:
    """Lazy-loading, disk-cached client for TCGTracking pricing data."""

    def __init__(self, cache_dir: Path = Path("data/tcgtracking")):
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "manabot/1.0"
        # populated on first call to _ensure_sets()
        self._set_index: dict[str, int] = {}          # UPPER(abbreviation) → set_id
        # populated when a set is loaded
        self._loaded_sets: set[str] = set()           # upper set codes already fetched
        self._product_index: dict[str, int] = {}      # scryfall_id → product_id
        self._sku_index: dict[int, list[TCGSKUPricing]] = {}  # product_id → skus

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, name: str) -> Path:
        return self._cache_dir / name

    def _load_json(self, url: str, cache_name: str, max_age_hours: float) -> dict | list:
        cache = self._cache_path(cache_name)
        if cache.exists():
            age_hours = (time.time() - cache.stat().st_mtime) / 3600
            if age_hours < max_age_hours:
                return json.loads(cache.read_bytes())
        resp = self._session.get(f"{_BASE}{url}", timeout=60)
        resp.raise_for_status()
        data = resp.json()
        cache.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        return data

    def _ensure_sets(self) -> None:
        if self._set_index:
            return
        data = self._load_json(f"/v1/{_MTG_CAT}/sets", "mtg_sets.json", _CACHE_SETS_HOURS)
        sets_list: list = data if isinstance(data, list) else data.get("sets", [])
        for s in sets_list:
            abbr = (s.get("abbreviation") or "").strip().upper()
            sid = s.get("id")
            if abbr and sid:
                self._set_index[abbr] = sid
        log.debug("TCGTracking: indexed %d MTG set abbreviations", len(self._set_index))

    def _load_set(self, set_code: str) -> bool:
        """Fetch and index cards + SKUs for *set_code*. Returns False if not found."""
        self._ensure_sets()
        key = set_code.upper()
        if key in self._loaded_sets:
            return key in {k for k, v in self._set_index.items() if v}

        # Mark as attempted before any network call so failures don't retry
        self._loaded_sets.add(key)

        set_id = self._set_index.get(key)
        if not set_id and key.startswith("T") and len(key) > 1:
            # Token sets use T[parent] naming (e.g. T40K → 40K). TCGTracking files
            # tokens under the parent set, so fall back to looking it up there.
            parent_key = key[1:]
            set_id = self._set_index.get(parent_key)
            if set_id:
                log.debug("TCGTracking: %s not found, falling back to parent set %s", set_code, parent_key)
        if not set_id:
            log.debug("TCGTracking: set %s not found in abbreviation index", set_code)
            return False

        # Cards: build scryfall_id → product_id mapping
        cards_data = self._load_json(
            f"/v1/{_MTG_CAT}/sets/{set_id}/cards",
            f"set_{set_id}_cards.json",
            _CACHE_CARDS_HOURS,
        )
        for product in (cards_data.get("products") or []):
            sid = product.get("scryfall_id")
            pid = product.get("id")
            if sid and pid:
                self._product_index[sid] = pid

        # SKUs: build product_id → [TCGSKUPricing] mapping
        skus_data = self._load_json(
            f"/v1/{_MTG_CAT}/sets/{set_id}/skus",
            f"set_{set_id}_skus.json",
            _CACHE_SKUS_HOURS,
        )
        for product_id_str, sku_map in (skus_data.get("products") or {}).items():
            pid = int(product_id_str)
            sku_list: list[TCGSKUPricing] = []
            for sku in sku_map.values():
                try:
                    sku_list.append(TCGSKUPricing(
                        condition=sku.get("cnd", ""),
                        finish=sku.get("var", "Normal"),
                        low=float(sku.get("low") or 0),
                        market=float(sku.get("mkt") or 0),
                        high=float(sku.get("hi") or 0),
                        listing_count=int(sku.get("cnt") or 0),
                        mp_price=float(sku["mp"]) if sku.get("mp") else None,
                    ))
                except (ValueError, KeyError):
                    continue
            if sku_list:
                self._sku_index[pid] = sku_list

        log.debug("TCGTracking: loaded set %s (id=%d)", set_code, set_id)
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_sku(
        self,
        scryfall_id: str,
        set_code: str,
        condition: str,    # "NM", "LP", "MP", "HP", "DMG"
        finish: str,       # "nonfoil", "foil", "etched"
    ) -> Optional[TCGSKUPricing]:
        """Return per-condition/finish TCGPlayer pricing, or None if not found."""
        self._load_set(set_code)
        product_id = self._product_index.get(scryfall_id)
        if not product_id:
            return None
        skus = self._sku_index.get(product_id)
        if not skus:
            return None
        tcg_finish = _FINISH_TO_TCG.get(finish, "Normal")
        for sku in skus:
            if sku.condition == condition and sku.finish == tcg_finish:
                return sku
        return None

    def get_market_price(
        self,
        scryfall_id: str,
        set_code: str,
        condition: str,
        finish: str,
    ) -> Optional[float]:
        """Return TCGPlayer market price for a variant, or None if unavailable."""
        sku = self.get_sku(scryfall_id, set_code, condition, finish)
        return sku.market if sku and sku.market > 0 else None
