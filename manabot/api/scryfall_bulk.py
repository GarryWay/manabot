"""Scryfall oracle_cards bulk data — price reference and sanction filtering.

Loads data/scryfall_oracle.json (one record per unique card name) and exposes:
  - is_sanctioned(card_name)  — True if legal in at least one tournament format
  - get_market_price(card_name, foil)  — TCGPlayer market price from Scryfall

The ~170 MB file is parsed once and cached in the instance. Load time ~2-3s.
Call download_oracle_cards() to fetch or refresh the file; the bot does this
automatically at startup and weekly thereafter.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/scryfall_oracle.json")
DEFAULT_CARDS_PATH = Path("data/scryfall_default_cards.jsonl.gz")

_BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
_HEADERS = {"User-Agent": "manabot/1.0"}


def _bulk_entry(bulk_type: str) -> dict:
    """Return Scryfall's bulk-data index entry for ``bulk_type`` (tiny JSON request)."""
    import requests

    resp = requests.get(_BULK_DATA_URL, timeout=30, headers=_HEADERS)
    resp.raise_for_status()
    entry = next((item for item in resp.json().get("data", []) if item["type"] == bulk_type), None)
    if entry is None:
        raise RuntimeError(f"{bulk_type} entry not found in Scryfall bulk-data listing")
    return entry


def read_bulk_meta(path: Path) -> Optional[dict]:
    """Return the ``<path>.meta.json`` sidecar written alongside a bulk download, if any."""
    meta_path = _meta_path(path)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _meta_path(path: Path) -> Path:
    name = path.name
    for suffix in (".jsonl.gz", ".json"):
        if name.endswith(suffix):
            return path.with_name(name[: -len(suffix)] + ".meta.json")
    return path.with_name(path.stem + ".meta.json")


def _is_current(path: Path, updated_at: str) -> bool:
    stored = read_bulk_meta(path)
    return path.exists() and stored is not None and stored.get("updated_at") == updated_at


def _download_to(uri: str, dest: Path) -> None:
    """Stream ``uri`` to ``dest`` via a temp file so a failed download never clobbers it."""
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with requests.get(uri, stream=True, timeout=120, headers=_HEADERS) as dl:
            dl.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in dl.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write_meta(path: Path, updated_at: str, uri: str) -> None:
    _meta_path(path).write_text(
        json.dumps({"updated_at": updated_at, "download_uri": uri}, indent=2),
        encoding="utf-8",
    )


def download_oracle_cards(path: Path = DEFAULT_PATH, force: bool = False) -> bool:
    """Download the Scryfall oracle_cards bulk file if a newer version is available.

    Fetches the bulk-data index (tiny JSON) on every call to check the current
    ``updated_at`` timestamp. The actual file is only downloaded when Scryfall has
    published a newer version than the one recorded in the sidecar
    ``<path>.meta.json``, or when the file is absent.

    Scryfall now publishes bulk files only as gzipped JSONL (``jsonl_download_uri``;
    the plain-JSON ``download_uri`` disappeared from the index around 2026-10), so
    the JSONL is converted to the JSON array that ScryfallBulk and the buylist
    scripts load. A plain ``download_uri`` is still used if Scryfall lists one.

    Returns True if the file was downloaded, False if already current.
    Raises ``requests.HTTPError`` or ``OSError`` on failure.
    """
    entry = _bulk_entry("oracle_cards")
    updated_at: str = entry["updated_at"]
    if not force and _is_current(path, updated_at):
        log.info("Scryfall oracle data is current (updated_at=%s)", updated_at)
        return False

    jsonl_uri = entry.get("jsonl_download_uri")
    uri: str = jsonl_uri or entry["download_uri"]
    log.info("Downloading Scryfall oracle_cards updated_at=%s ...", updated_at)
    if jsonl_uri:
        gz_path = path.with_name(path.name + ".jsonl.gz")
        _download_to(jsonl_uri, gz_path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as out:
                out.write("[")
                first = True
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    out.write(line if first else ",\n" + line)
                    first = False
                out.write("]")
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            gz_path.unlink(missing_ok=True)
    else:
        _download_to(uri, path)

    _write_meta(path, updated_at, uri)
    log.info("Scryfall oracle_cards saved to %s", path)
    return True


def download_default_cards(path: Path = DEFAULT_CARDS_PATH, force: bool = False) -> bool:
    """Download Scryfall's default_cards bulk file (every printing, ~80 MB gzipped JSONL).

    Kept gzipped on disk; read it with iter_bulk_jsonl(). Same freshness check as
    download_oracle_cards(): the sidecar meta's ``updated_at`` is also the price
    date the history archive records for this file.

    Returns True if the file was downloaded, False if already current.
    """
    entry = _bulk_entry("default_cards")
    updated_at: str = entry["updated_at"]
    if not force and _is_current(path, updated_at):
        log.info("Scryfall default_cards is current (updated_at=%s)", updated_at)
        return False

    uri: str = entry["jsonl_download_uri"]
    log.info("Downloading Scryfall default_cards updated_at=%s ...", updated_at)
    _download_to(uri, path)
    _write_meta(path, updated_at, uri)
    log.info("Scryfall default_cards saved to %s", path)
    return True


def iter_bulk_jsonl(path: Path) -> Iterator[dict]:
    """Yield card objects one at a time from a gzipped JSONL bulk file."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

# Formats whose legality makes a card resalable as a tournament playable card.
_SANCTIONED_FORMATS = {"vintage", "legacy", "commander", "modern", "pioneer", "standard"}

# Scryfall layouts where at least one face is a token — optimizer rejects these
# because they're indexed with is_token=true in ManaPool's inventory.
_TOKEN_LAYOUTS = {"token", "double_faced_token"}

# Scryfall set_type values that indicate non-tournament-legal product.
# "memorabilia" covers WC04/CE/IE gold-border cards; "funny" covers Un-sets;
# "token" covers token sheets; "minigame" covers oversized game pieces.
_NON_PLAYABLE_SET_TYPES = {"memorabilia", "funny", "token", "minigame"}


class ScryfallBulk:
    def __init__(self, path: Path = DEFAULT_PATH):
        self._path = path
        # card_name (lowered) → (usd, usd_foil, sanctioned)
        self._data: dict[str, tuple[Optional[float], Optional[float], bool]] = {}
        self._token_names: set[str] = set()
        # set_code (lowered) → release date
        self._set_release_dates: dict[str, date] = {}
        # set_code (lowered) → Scryfall set_type string
        self._set_types: dict[str, str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._path.exists():
            log.warning(
                "Scryfall bulk data not found at %s — sanctioned filtering and "
                "price lookups unavailable. Run scripts/populate_buylist.py to download.",
                self._path,
            )
            self._loaded = True
            return

        log.info("Loading Scryfall bulk data from %s ...", self._path)
        with self._path.open(encoding="utf-8") as f:
            cards = json.load(f)

        for card in cards:
            name: str = card.get("name", "")
            layout: str = card.get("layout", "")
            prices = card.get("prices", {})
            usd = float(prices["usd"]) if prices.get("usd") else None
            usd_foil = float(prices["usd_foil"]) if prices.get("usd_foil") else None
            legalities: dict = card.get("legalities", {})
            sanctioned = any(
                legalities.get(fmt) in ("legal", "restricted")
                for fmt in _SANCTIONED_FORMATS
            )
            entry = (usd, usd_foil, sanctioned)
            key = name.lower()
            self._data[key] = entry
            # DFC front-face alias so "Bala Ged Recovery" finds "Bala Ged Recovery // ..."
            if " // " in name:
                front = name.split(" // ")[0].lower()
                if front not in self._data:
                    self._data[front] = entry

            if layout in _TOKEN_LAYOUTS:
                self._token_names.add(key)
                if " // " in name:
                    self._token_names.add(name.split(" // ")[0].lower())

            set_code: str = card.get("set", "")
            released_at_str: str = card.get("released_at", "")
            if set_code and set_code.lower() not in self._set_release_dates:
                if released_at_str:
                    try:
                        self._set_release_dates[set_code.lower()] = date.fromisoformat(released_at_str)
                    except ValueError:
                        pass
                set_type: str = card.get("set_type", "")
                if set_type:
                    self._set_types[set_code.lower()] = set_type

        log.info("Loaded %d Scryfall card entries (%d sets)", len(self._data), len(self._set_release_dates))
        self._loaded = True

    def is_sanctioned(self, card_name: str) -> bool:
        """Return True if the card is legal in at least one sanctioned tournament format.

        Cards not found in the bulk data are assumed sanctioned to avoid false exclusions.
        """
        self._ensure_loaded()
        entry = self._data.get(card_name.lower())
        if entry is None:
            return True  # unknown → don't exclude
        return entry[2]

    def is_token(self, card_name: str) -> bool:
        """Return True if the card has a token or double_faced_token layout.

        ManaPool's optimizer indexes these with is_token=true, so sending them
        with is_token=false (our default) always results in a 409.
        ManaPool may append a token face to the name (e.g. "Stoneforged Blade // Germ")
        while oracle_cards stores only the front face ("Stoneforged Blade"), so we
        check both the full name and the front face.
        Cards not found in the bulk data are assumed non-token.
        """
        self._ensure_loaded()
        key = card_name.lower()
        if key in self._token_names:
            return True
        if " // " in card_name:
            front = card_name.split(" // ")[0].lower()
            return front in self._token_names
        return False

    def is_recently_released(self, set_code: str, days: int = 30) -> bool:
        """Return True if the set was released within the last `days` days.

        Covers both presale sets (future release dates) and newly released sets.
        Unknown set codes return False so that unrecognized sets are not excluded.
        """
        self._ensure_loaded()
        release_date = self._set_release_dates.get(set_code.lower())
        if release_date is None:
            return False  # unknown set → don't exclude
        return (date.today() - release_date).days < days

    def is_playable_set(self, set_code: str) -> bool:
        """Return True if cards from this set are suitable for tournament and optimizer use.

        Excludes memorabilia sets (WC97-WC04, Collector's Edition, etc.), Un-sets, token
        sheets, and minigame products. Unknown set codes return True to avoid over-excluding.
        """
        self._ensure_loaded()
        set_type = self._set_types.get(set_code.lower(), "")
        return set_type not in _NON_PLAYABLE_SET_TYPES

    def get_market_price(self, card_name: str, foil: bool = False) -> Optional[float]:
        """Return TCGPlayer market price from Scryfall for this card."""
        self._ensure_loaded()
        entry = self._data.get(card_name.lower())
        if entry is None:
            return None
        return entry[1] if foil else entry[0]

    @property
    def available(self) -> bool:
        """True if the bulk data file was found and loaded."""
        self._ensure_loaded()
        return bool(self._data)
