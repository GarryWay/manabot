from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Promo type values that indicate a non-in-universe (alternate name/universe) printing
_ALT_UNIVERSE_PROMO_TYPES = {"universesbeyond", "sourcematerial"}


class ScryfallAPIError(Exception):
    pass


class ScryfallClient:
    """Scryfall API client with in-memory caching and rate limiting.

    Scryfall guidelines: max 10 req/sec, be respectful. We enforce 100ms
    between requests to stay well within limits.
    """
    BASE_URL = "https://api.scryfall.com"
    _MIN_DELAY = 0.1  # seconds between requests

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "manabot/0.1.0 (price-monitoring-bot)"})
        self._cache: dict[str, dict] = {}
        self._last_request_time: float = 0.0

    def lookup_by_name(self, name: str) -> Optional[str]:
        """Return the Scryfall ID for a card name, or None if not found.

        Tries exact match first; falls back to fuzzy. Returns the ID of the
        most iconic/default printing (Scryfall's choice).
        """
        for strategy, params in [("exact", {"exact": name}), ("fuzzy", {"fuzzy": name})]:
            try:
                data = self._get("/cards/named", params=params)
                found_id = data.get("id")
                if found_id:
                    log.debug("Resolved %r (%s match) → %s", name, strategy, found_id)
                    return found_id
            except ScryfallAPIError as e:
                if "404" in str(e):
                    continue  # try fuzzy next
                log.warning("Scryfall name lookup failed for %r: %s", name, e)
                return None
        log.warning("Could not resolve Scryfall ID for %r", name)
        return None

    def get_card_metadata(self, scryfall_id: str) -> dict:
        """Fetch full card metadata for a Scryfall ID. Results are cached."""
        if scryfall_id in self._cache:
            return self._cache[scryfall_id]
        data = self._get(f"/cards/{scryfall_id}")
        self._cache[scryfall_id] = data
        return data

    def is_in_universe(self, scryfall_id: str) -> Optional[bool]:
        """Return True if this is a standard in-universe printing.

        Returns False when:
          - `flavor_name` is set (card has an alternate universe name printed on it,
            e.g. "Wild Rose Rebellion" instead of "Counterspell")
          - `promo_types` contains "universesbeyond" or "sourcematerial"

        Returns None if the metadata fetch fails — callers should treat None as
        "include with warning" rather than silently excluding.
        """
        try:
            meta = self.get_card_metadata(scryfall_id)
        except ScryfallAPIError as e:
            log.warning("Could not fetch Scryfall metadata for %s: %s", scryfall_id, e)
            return None

        if meta.get("flavor_name"):
            log.debug(
                "Excluding %s: flavor_name=%r (alternate universe name)",
                scryfall_id, meta["flavor_name"],
            )
            return False

        promo_types = set(meta.get("promo_types") or [])
        bad_types = promo_types & _ALT_UNIVERSE_PROMO_TYPES
        if bad_types:
            log.debug("Excluding %s: promo_types=%s", scryfall_id, bad_types)
            return False

        return True

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        self._rate_limit()
        url = f"{self.BASE_URL}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise ScryfallAPIError(f"HTTP {e.response.status_code} from {url}") from e
        except requests.ConnectionError as e:
            raise ScryfallAPIError(f"Connection error fetching {url}") from e
        except requests.Timeout as e:
            raise ScryfallAPIError(f"Timeout fetching {url}") from e
        finally:
            self._last_request_time = time.monotonic()
        return resp.json()

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._MIN_DELAY:
            time.sleep(self._MIN_DELAY - elapsed)
