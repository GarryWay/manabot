"""
ManaPool API client.

Field name mapping lives entirely in _parse_listing() / _parse_listing_csv().
If the API returns different field names than expected, update those two methods.
Run `python -m manabot run --verbose` after first auth to see raw response structure.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from manabot.models import Condition, Finish, PriceListing

log = logging.getLogger(__name__)

# Header names as specified in ManaPool OpenAPI securitySchemes
_EMAIL_HEADER = "Email"
_TOKEN_HEADER = "Access-Token"

# Mapping from ManaPool condition strings to our Condition enum.
# Update if the API uses different values.
_CONDITION_MAP: dict[str, Condition] = {
    "NM": Condition.NM,
    "Near Mint": Condition.NM,
    "LP": Condition.LP,
    "Lightly Played": Condition.LP,
    "MP": Condition.MP,
    "Moderately Played": Condition.MP,
    "HP": Condition.HP,
    "Heavily Played": Condition.HP,
    "DMG": Condition.DMG,
    "Damaged": Condition.DMG,
}

_FINISH_MAP: dict[str, Finish] = {
    "foil": Finish.FOIL,
    "nonfoil": Finish.NONFOIL,
    "non_foil": Finish.NONFOIL,
    "normal": Finish.NONFOIL,
}


class ManaPoolAPIError(Exception):
    """Raised for HTTP errors and network failures from the ManaPool API."""


class ManaPoolClient:
    BASE_URL = "https://manapool.com/api/v1"

    def __init__(self, email: str, token: str, use_bulk_export: bool = False) -> None:
        self.use_bulk_export = use_bulk_export
        self._session = requests.Session()
        self._session.headers.update({
            _EMAIL_HEADER: email,
            _TOKEN_HEADER: token,
        })

    def get_singles_prices(self) -> list[PriceListing]:
        """Fetch all in-stock singles prices. Uses bulk export if configured."""
        if self.use_bulk_export:
            return self._get_singles_bulk()
        return self._get_singles_live()

    def _get_singles_live(self) -> list[PriceListing]:
        raw = self._get("/prices/singles")
        log.debug("Raw singles response keys (first item): %s", list(raw[0].keys()) if raw else "empty")
        fetched_at = datetime.now(timezone.utc)
        return [self._parse_listing(item, fetched_at) for item in raw]

    def _get_singles_bulk(self) -> list[PriceListing]:
        """Fetch the daily gzip export for more efficient bulk pulls."""
        raw_bytes = self._get_raw("/prices/singles", headers={"Accept": "application/octet-stream"})
        fetched_at = datetime.now(timezone.utc)
        listings: list[PriceListing] = []
        with gzip.open(io.BytesIO(raw_bytes)) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                try:
                    listings.append(self._parse_listing_csv(row, fetched_at))
                except (KeyError, ValueError) as e:
                    log.warning("Skipping unparseable bulk row: %s", e)
        return listings

    # ------------------------------------------------------------------
    # Field mapping — update here if ManaPool changes its response shape
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_listing(raw: dict[str, Any], fetched_at: datetime) -> PriceListing:
        """Map a raw JSON dict from GET /prices/singles to a PriceListing.

        Field names below are best-guess from API docs. Verify with a real
        response and update if needed — this is the only place that needs changing.
        """
        condition_str = raw.get("condition", "")
        condition = _CONDITION_MAP.get(condition_str, Condition.LP)

        finish_str = str(raw.get("finish", raw.get("foil", "nonfoil"))).lower()
        finish = _FINISH_MAP.get(finish_str, Finish.NONFOIL)

        return PriceListing(
            scryfall_id=str(raw.get("scryfall_id", raw.get("scryfallId", ""))),
            card_name=str(raw.get("name", raw.get("card_name", ""))),
            set_code=str(raw.get("set", raw.get("set_code", ""))).upper(),
            condition=condition,
            finish=finish,
            price_usd=float(raw.get("price", raw.get("price_usd", 0.0))),
            quantity_available=int(raw.get("quantity", raw.get("qty", 0))),
            seller_id=str(raw.get("seller_id", raw.get("sellerId", ""))),
            fetched_at=fetched_at,
        )

    @staticmethod
    def _parse_listing_csv(row: dict[str, str], fetched_at: datetime) -> PriceListing:
        """Map a CSV row from the bulk export to a PriceListing."""
        condition_str = row.get("condition", "")
        condition = _CONDITION_MAP.get(condition_str, Condition.LP)

        finish_str = row.get("finish", row.get("foil", "nonfoil")).lower()
        finish = _FINISH_MAP.get(finish_str, Finish.NONFOIL)

        return PriceListing(
            scryfall_id=row.get("scryfall_id", row.get("scryfallId", "")),
            card_name=row.get("name", row.get("card_name", "")),
            set_code=row.get("set", row.get("set_code", "")).upper(),
            condition=condition,
            finish=finish,
            price_usd=float(row.get("price", row.get("price_usd", 0))),
            quantity_available=int(row.get("quantity", row.get("qty", 0))),
            seller_id=row.get("seller_id", row.get("sellerId", "")),
            fetched_at=fetched_at,
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}{path}"
        try:
            resp = self._session.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(f"HTTP {e.response.status_code} from {url}: {e.response.text[:200]}") from e
        except requests.ConnectionError as e:
            raise ManaPoolAPIError(f"Connection error fetching {url}") from e
        except requests.Timeout as e:
            raise ManaPoolAPIError(f"Timeout fetching {url}") from e
        return resp.json()

    def _get_raw(self, path: str, headers: Optional[dict] = None) -> bytes:
        url = f"{self.BASE_URL}{path}"
        try:
            resp = self._session.get(url, timeout=60, headers=headers or {})
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(f"HTTP {e.response.status_code} from {url}") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError(f"Network error fetching {url}") from e
        return resp.content
