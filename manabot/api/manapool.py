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
import json
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


class ManaPool409Error(ManaPoolAPIError):
    """Raised when the optimizer returns 409 — specific items could not be resolved.

    unresolvable_names contains the card names extracted from the 'details' array.
    """
    def __init__(self, message: str, unresolvable_names: list[str]) -> None:
        super().__init__(message)
        self.unresolvable_names = unresolvable_names


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
        # API wraps results in {"meta": {...}, "data": [...]}
        if isinstance(raw, dict) and "data" in raw:
            items = raw["data"]
        elif isinstance(raw, list):
            items = raw
        else:
            raise ManaPoolAPIError(
                f"Unexpected response shape from GET /prices/singles: {type(raw).__name__}. "
                f"Response: {str(raw)[:300]}. "
                f"Check your MANAPOOL_EMAIL and MANAPOOL_TOKEN credentials."
            )
        if items:
            log.debug("Raw singles response keys (first item): %s", list(items[0].keys()))
        fetched_at = datetime.now(timezone.utc)
        listings: list[PriceListing] = []
        for item in items:
            listings.extend(self._expand_listings(item, fetched_at))
        return listings

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

        Verified field names from live API (v0.27.0):
          price_cents        — price in cents (integer); divide by 100 for USD
          available_quantity — total quantity in stock
          set_code           — set abbreviation
          scryfall_id        — Scryfall UUID
          name               — card name

        Condition and finish field names are not yet confirmed from a live response.
        Update the lookups below once verified.
        """
        condition_str = raw.get("condition", "")
        condition = _CONDITION_MAP.get(condition_str, Condition.LP)

        finish_str = str(raw.get("finish", raw.get("foil", "nonfoil"))).lower()
        finish = _FINISH_MAP.get(finish_str, Finish.NONFOIL)

        # price_cents is the confirmed live field; fall back to price/price_usd for future changes
        price_cents = raw.get("price_cents")
        if price_cents is not None:
            price_usd = float(price_cents) / 100.0
        else:
            price_usd = float(raw.get("price", raw.get("price_usd", 0.0)))

        return PriceListing(
            scryfall_id=str(raw.get("scryfall_id", raw.get("scryfallId", ""))),
            card_name=str(raw.get("name", raw.get("card_name", ""))),
            set_code=str(raw.get("set_code", raw.get("set", ""))).upper(),
            condition=condition,
            finish=finish,
            price_usd=price_usd,
            quantity_available=int(raw.get("available_quantity", raw.get("quantity", raw.get("qty", 0)))),
            seller_id=str(raw.get("seller_id", raw.get("sellerId", ""))),
            fetched_at=fetched_at,
        )

    @staticmethod
    def _expand_listings(raw: dict[str, Any], fetched_at: datetime) -> list[PriceListing]:
        """Expand one API aggregate row into separate condition/finish tier listings.

        ManaPool returns one row per card printing with separate price fields for
        each condition tier and finish:
          price_cents_nm        / price_cents_nm_foil        — NM copies
          price_cents_lp_plus   / price_cents_lp_plus_foil   — LP-or-better copies
          price_cents           / price_cents_foil            — any-condition copies

        A value of 0 means no copies exist at that tier/finish; only non-zero fields
        produce listings. Condition is assigned to match the tier semantics so the
        matcher's condition filter works correctly (e.g. a buyer requiring NM only
        matches the NM-tier listing).
        """
        # price_market / price_market_foil use the same cents convention as price_cents_*
        def _market(val: Any) -> Optional[float]:
            try:
                cents = float(val)
                return (cents / 100.0) if cents > 0 else None
            except (TypeError, ValueError):
                return None

        market_nonfoil = _market(raw.get("price_market"))
        market_foil = _market(raw.get("price_market_foil"))

        base = dict(
            scryfall_id=str(raw.get("scryfall_id", "")),
            card_name=str(raw.get("name", "")),
            set_code=str(raw.get("set_code", raw.get("set", ""))).upper(),
            quantity_available=int(raw.get("available_quantity", 0) or 0),
            seller_id=str(raw.get("seller_id", "")),
            fetched_at=fetched_at,
            url=str(raw.get("url", "")),
        )

        tiers: list[tuple[str, Condition, Finish]] = [
            ("price_cents_nm",           Condition.NM, Finish.NONFOIL),
            ("price_cents_lp_plus",      Condition.LP, Finish.NONFOIL),
            ("price_cents",              Condition.MP, Finish.NONFOIL),
            ("price_cents_nm_foil",      Condition.NM, Finish.FOIL),
            ("price_cents_lp_plus_foil", Condition.LP, Finish.FOIL),
            ("price_cents_foil",         Condition.MP, Finish.FOIL),
        ]

        listings: list[PriceListing] = []
        for field, condition, finish in tiers:
            cents = raw.get(field, 0) or 0
            if cents > 0:
                market = market_foil if finish == Finish.FOIL else market_nonfoil
                listings.append(PriceListing(
                    price_usd=float(cents) / 100.0,
                    condition=condition,
                    finish=finish,
                    market_price_usd=market,
                    **base,
                ))
        return listings

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

    def run_optimizer(
        self,
        cart: list[dict],
        model: str = "lowest_price",
        destination_country: str = "US",
        ship_from_countries: list[str] | None = None,
        exclude_universes_beyond: bool = False,
        exclude_preorder: bool = False,
    ) -> dict:
        """POST /buyer/optimizer — streams NDJSON and returns the last (best) cart.

        The endpoint emits progressively optimized carts as newline-delimited JSON.
        We consume the full stream and return the last cart object, which represents
        the most optimized result. Stats summary lines are skipped.
        """
        url = f"{self.BASE_URL}/buyer/optimizer"
        payload: dict = {
            "cart": cart,
            "model": model,
            "destination_country": destination_country,
            "ship_from_countries": ship_from_countries if ship_from_countries is not None else ["US", "CA"],
            "include_replacement_warehouses": False,
        }
        single_filters: dict = {}
        if exclude_universes_beyond:
            single_filters["excludeUniversesBeyond"] = True
        if exclude_preorder:
            single_filters["excludePreRelease"] = True
        if single_filters:
            payload["filters"] = {"productFilters": {"singleFilters": single_filters}}

        try:
            resp = self._session.post(
                url, json=payload, stream=True, timeout=120,
                headers={"Accept": "application/x-ndjson"},
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            if e.response.status_code == 409:
                try:
                    body = e.response.json()
                    names = [
                        d["item"]["name"]
                        for d in body.get("details", [])
                        if isinstance(d, dict) and "item" in d and "name" in d["item"]
                    ]
                except Exception:
                    names = []
                raise ManaPool409Error(
                    f"HTTP 409 from optimizer: could not resolve {names or 'unknown items'}",
                    unresolvable_names=names,
                ) from e
            raise ManaPoolAPIError(
                f"HTTP {e.response.status_code} from POST /buyer/optimizer: {e.response.text[:200]}"
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError("Network error calling optimizer") from e

        best: dict | None = None
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Unparseable optimizer response line: %r", line[:100])
                continue
            if "stats" in data:
                continue  # skip stats summary objects
            if "cart" in data:
                best = data  # each successive cart is better; keep the last one

        if best is None:
            raise ManaPoolAPIError(
                "Optimizer returned no valid cart. "
                "Check that your cart items are available on ManaPool."
            )
        return best

    def create_pending_order(
        self,
        raw_cart: list[dict],
        shipping_address: dict | None = None,
    ) -> dict:
        """POST /buyer/orders/pending-orders — create a pending order for human review.

        raw_cart is the list of {inventory_id, quantity_selected} dicts returned
        by run_optimizer(). Creates the order in "pending" status — it does NOT
        charge or process a payment. Call purchase_order() to finalise.

        shipping_address (optional): dict with line1, city, state, postal_code, country.
        If provided, taxes are calculated against it. Required before purchase.

        Response fields: id, status, totals (subtotal_cents, shipping_cents,
        tax_cents, total_cents), order (null while pending).
        """
        url = f"{self.BASE_URL}/buyer/orders/pending-orders"
        payload: dict = {"line_items": raw_cart}
        if shipping_address:
            payload["shipping_address"] = shipping_address
        try:
            resp = self._session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(
                f"HTTP {e.response.status_code} from POST /buyer/orders/pending-orders: {e.response.text[:200]}"
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError("Network error creating pending order") from e
        return resp.json()

    def purchase_order(
        self,
        pending_order_id: str,
        payment_method: str,
        billing_address: dict,
        shipping_address: dict,
    ) -> dict:
        """POST /buyer/orders/pending-orders/{id}/purchase — finalise a pending order.

        This charges the account and places the order. Only call after human review.

        payment_method: currently only "user_credit" is supported by the API.
        billing_address / shipping_address: dicts with line1, city, state (2-char),
        postal_code, country ("US" or "CA").

        Response: same pending-order shape with status="completed" and
        order.id set to the created buyer order ID.
        """
        url = f"{self.BASE_URL}/buyer/orders/pending-orders/{pending_order_id}/purchase"
        payload = {
            "payment_method": payment_method,
            "billing_address": billing_address,
            "shipping_address": shipping_address,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(
                f"HTTP {e.response.status_code} from POST /buyer/orders/pending-orders/{pending_order_id}/purchase: {e.response.text[:200]}"
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError("Network error purchasing order") from e
        return resp.json()

    def get_inventory_details(self, inventory_ids: list[str]) -> list[dict]:
        """GET /inventory/listings?id[]=... — resolve inventory IDs to card details.

        Returns a list of inventory_item dicts. Each has:
          id, price_cents, product.single.{name, set, condition_id, finish_id}
        Unknown IDs are silently omitted by the API.
        """
        if not inventory_ids:
            return []
        try:
            resp = self._session.get(
                f"{self.BASE_URL}/inventory/listings",
                params={"id": inventory_ids},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(
                f"HTTP {e.response.status_code} from GET /inventory/listings: {e.response.text[:200]}"
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError("Network error fetching inventory details") from e
        return resp.json().get("inventory_items", [])

    def get_pending_order(self, pending_order_id: str) -> dict:
        """GET /buyer/orders/pending-orders/{id} — fetch a pending order by ID."""
        url = f"{self.BASE_URL}/buyer/orders/pending-orders/{pending_order_id}"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ManaPoolAPIError(
                f"HTTP {e.response.status_code} from GET /buyer/orders/pending-orders/{pending_order_id}: {e.response.text[:400]}"
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ManaPoolAPIError("Network error fetching pending order") from e
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
