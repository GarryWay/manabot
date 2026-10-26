"""Daily price history archive — the training data for price forecasts.

Neither source keeps history for us: Scryfall's bulk prices and ManaPool's catalog
are both "today only" snapshots, and the catalog's recent_sales holds only the
last 20 sales per variant. So this module archives each day's snapshot into its
own SQLite file (``history_db_path``, separate from manabot.db because it grows by
tens of thousands of rows a day):

  printings       — one row per Scryfall printing: the static metadata forecasts
                    group cards by (set, rarity, release date, treatment, reprint).
  scryfall_daily  — Scryfall's daily prices per printing (usd = TCGPlayer market).
  manapool_daily  — ManaPool catalog per printing: market, lowest asks, supply.
  manapool_sales  — every sale seen in the catalog's recent_sales, deduplicated.
                    Archived daily, the 20-sale window becomes a complete sales
                    log (almost no variant sells 20+ copies in a day). The first
                    ingest also captures whatever older sales the window holds.
  ingest_runs     — one row per (source, as_of) so a day is never ingested twice.

Prices are stored as integer cents. Daily rows below ``min_price_usd`` (the
printing's highest price across finishes) are skipped — bulk cards are not worth
forecasting and would otherwise dominate the archive's size. Sales are kept for
the same printings.
"""
from __future__ import annotations

import gzip
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable, Optional

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS printings (
    id               INTEGER PRIMARY KEY,
    scryfall_id      TEXT NOT NULL UNIQUE,
    oracle_id        TEXT,
    name             TEXT,
    set_code         TEXT,
    collector_number TEXT,
    set_type         TEXT,
    rarity           TEXT,
    released_at      TEXT,
    layout           TEXT,
    border_color     TEXT,
    frame            TEXT,
    frame_effects    TEXT,    -- comma-joined
    promo_types      TEXT,    -- comma-joined
    finishes         TEXT,    -- comma-joined
    full_art         INTEGER,
    promo            INTEGER,
    reprint          INTEGER,
    reserved         INTEGER,
    lang             TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_printings_oracle ON printings (oracle_id);
CREATE INDEX IF NOT EXISTS idx_printings_set ON printings (set_code);

CREATE TABLE IF NOT EXISTS scryfall_daily (
    printing_id       INTEGER NOT NULL,
    as_of             TEXT    NOT NULL,   -- YYYY-MM-DD of Scryfall's bulk updated_at
    usd_cents         INTEGER,
    usd_foil_cents    INTEGER,
    usd_etched_cents  INTEGER,
    eur_cents         INTEGER,
    eur_foil_cents    INTEGER,
    tix_cents         INTEGER,
    edhrec_rank       INTEGER,
    PRIMARY KEY (printing_id, as_of)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS manapool_daily (
    printing_id          INTEGER NOT NULL,
    as_of                TEXT    NOT NULL,   -- YYYY-MM-DD the catalog was downloaded (UTC)
    market_cents         INTEGER,
    market_foil_cents    INTEGER,
    low_cents            INTEGER,            -- lowest ask, any condition
    low_foil_cents       INTEGER,
    low_etched_cents     INTEGER,
    nm_cents             INTEGER,            -- lowest NM ask
    nm_foil_cents        INTEGER,
    nm_etched_cents      INTEGER,
    lp_plus_cents        INTEGER,            -- lowest LP-or-better ask
    lp_plus_foil_cents   INTEGER,
    lp_plus_etched_cents INTEGER,
    available_quantity   INTEGER,
    PRIMARY KEY (printing_id, as_of)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS manapool_sales (
    printing_id  INTEGER NOT NULL,
    condition_id TEXT    NOT NULL,
    finish_id    TEXT    NOT NULL,
    language_id  TEXT    NOT NULL,
    sold_at      TEXT    NOT NULL,
    price_cents  INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    PRIMARY KEY (printing_id, condition_id, finish_id, language_id, sold_at, price_cents, quantity)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingest_runs (
    source       TEXT NOT NULL,   -- 'scryfall' | 'manapool'
    as_of        TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    daily_rows   INTEGER NOT NULL,
    new_sales    INTEGER,
    PRIMARY KEY (source, as_of)
);
"""

_BATCH = 5000

_PRINTING_COLS = (
    "scryfall_id", "oracle_id", "name", "set_code", "collector_number", "set_type",
    "rarity", "released_at", "layout", "border_color", "frame", "frame_effects",
    "promo_types", "finishes", "full_art", "promo", "reprint", "reserved", "lang", "updated_at",
)


def init_history_db(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


@contextmanager
def open_history_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = init_history_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _usd_to_cents(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_cents(*values: Optional[int]) -> int:
    return max((v for v in values if v is not None), default=0)


def already_ingested(conn: sqlite3.Connection, source: str, as_of: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ingest_runs WHERE source = ? AND as_of = ?", (source, as_of)
    ).fetchone()
    return row is not None


def _record_run(conn: sqlite3.Connection, source: str, as_of: str, daily_rows: int,
                new_sales: Optional[int] = None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO ingest_runs (source, as_of, ingested_at, daily_rows, new_sales)
           VALUES (?, ?, ?, ?, ?)""",
        (source, as_of, datetime.now(timezone.utc).isoformat(), daily_rows, new_sales),
    )


def _printing_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute("SELECT scryfall_id, id FROM printings")}


def _flush(conn: sqlite3.Connection, sql: str, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(sql, rows)
        rows.clear()


# ── Scryfall ─────────────────────────────────────────────────────────────────

def _scryfall_oracle_id(card: dict) -> Optional[str]:
    # Reversible cards carry oracle_id per face rather than at the top level.
    if card.get("oracle_id"):
        return card["oracle_id"]
    faces = card.get("card_faces") or []
    return faces[0].get("oracle_id") if faces else None


def _printing_row(card: dict, updated_at: str) -> tuple:
    return (
        card["id"],
        _scryfall_oracle_id(card),
        card.get("name"),
        str(card.get("set", "")).upper(),
        card.get("collector_number"),
        card.get("set_type"),
        card.get("rarity"),
        card.get("released_at"),
        card.get("layout"),
        card.get("border_color"),
        card.get("frame"),
        ",".join(card.get("frame_effects") or []) or None,
        ",".join(card.get("promo_types") or []) or None,
        ",".join(card.get("finishes") or []) or None,
        int(bool(card.get("full_art"))),
        int(bool(card.get("promo"))),
        int(bool(card.get("reprint"))),
        int(bool(card.get("reserved"))),
        card.get("lang"),
        updated_at,
    )


def ingest_scryfall_cards(
    conn: sqlite3.Connection,
    cards: Iterable[dict],
    as_of: str,
    min_price_usd: float = 0.5,
    force: bool = False,
) -> Optional[int]:
    """Upsert printings metadata and archive one day of Scryfall prices.

    ``cards`` is the default_cards bulk file's card objects; ``as_of`` is the
    YYYY-MM-DD price date. Returns the number of daily price rows written, or None
    if this date was already ingested (and ``force`` is False).
    """
    if not force and already_ingested(conn, "scryfall", as_of):
        log.info("Scryfall prices for %s already archived — skipping", as_of)
        return None

    min_cents = round(min_price_usd * 100)
    placeholders = ", ".join("?" for _ in _PRINTING_COLS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _PRINTING_COLS[1:])
    printing_sql = (
        f"INSERT INTO printings ({', '.join(_PRINTING_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (scryfall_id) DO UPDATE SET {updates}"
    )
    daily_sql = (
        "INSERT OR REPLACE INTO scryfall_daily (printing_id, as_of, usd_cents, usd_foil_cents, "
        "usd_etched_cents, eur_cents, eur_foil_cents, tix_cents, edhrec_rank) "
        "SELECT id, ?, ?, ?, ?, ?, ?, ?, ? FROM printings WHERE scryfall_id = ?"
    )

    printing_rows: list[tuple] = []
    daily_rows: list[tuple] = []
    n_printings = n_daily = 0
    for card in cards:
        if not card.get("id"):
            continue
        printing_rows.append(_printing_row(card, as_of))
        n_printings += 1

        prices = card.get("prices") or {}
        usd = _usd_to_cents(prices.get("usd"))
        usd_foil = _usd_to_cents(prices.get("usd_foil"))
        usd_etched = _usd_to_cents(prices.get("usd_etched"))
        if _max_cents(usd, usd_foil, usd_etched) >= min_cents:
            daily_rows.append((
                as_of, usd, usd_foil, usd_etched,
                _usd_to_cents(prices.get("eur")),
                _usd_to_cents(prices.get("eur_foil")),
                _usd_to_cents(prices.get("tix")),
                _int_or_none(card.get("edhrec_rank")),
                card["id"],
            ))
            n_daily += 1

        if len(printing_rows) >= _BATCH:
            # Printings first: the daily insert looks up printing_id by scryfall_id.
            _flush(conn, printing_sql, printing_rows)
            _flush(conn, daily_sql, daily_rows)
    _flush(conn, printing_sql, printing_rows)
    _flush(conn, daily_sql, daily_rows)

    _record_run(conn, "scryfall", as_of, n_daily)
    conn.commit()
    log.info("Scryfall %s: %d printings upserted, %d daily price rows archived", as_of, n_printings, n_daily)
    return n_daily


def ingest_scryfall_file(conn: sqlite3.Connection, path: Path, min_price_usd: float = 0.5,
                         force: bool = False) -> Optional[int]:
    """Ingest a downloaded default_cards file, dated by its sidecar meta's updated_at."""
    from manabot.api.scryfall_bulk import iter_bulk_jsonl, read_bulk_meta

    meta = read_bulk_meta(path)
    if not meta or not meta.get("updated_at"):
        raise RuntimeError(f"No updated_at in the meta sidecar for {path} — re-download it")
    as_of = meta["updated_at"][:10]
    return ingest_scryfall_cards(conn, iter_bulk_jsonl(path), as_of, min_price_usd, force)


# ── ManaPool catalog ─────────────────────────────────────────────────────────

def ingest_manapool_records(
    conn: sqlite3.Connection,
    records: Iterable[dict],
    as_of: str,
    min_price_usd: float = 0.5,
    force: bool = False,
) -> Optional[tuple[int, int]]:
    """Archive one day of the ManaPool catalog: per-printing prices/supply plus new sales.

    ``records`` are the catalog's top-level card records (with ``variants``).
    Printings Scryfall hasn't supplied yet get a stub row (id, name, set, number).
    Returns (daily rows written, new sales inserted), or None if this date was
    already ingested (and ``force`` is False).
    """
    if not force and already_ingested(conn, "manapool", as_of):
        log.info("ManaPool catalog for %s already archived — skipping", as_of)
        return None

    min_cents = round(min_price_usd * 100)
    ids = _printing_ids(conn)
    daily_sql = (
        "INSERT OR REPLACE INTO manapool_daily (printing_id, as_of, market_cents, market_foil_cents, "
        "low_cents, low_foil_cents, low_etched_cents, nm_cents, nm_foil_cents, nm_etched_cents, "
        "lp_plus_cents, lp_plus_foil_cents, lp_plus_etched_cents, available_quantity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    sales_sql = (
        "INSERT OR IGNORE INTO manapool_sales (printing_id, condition_id, finish_id, language_id, "
        "sold_at, price_cents, quantity) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    sales_before = conn.execute("SELECT COUNT(*) FROM manapool_sales").fetchone()[0]

    daily_rows: list[tuple] = []
    sales_rows: list[tuple] = []
    n_daily = 0
    for r in records:
        scryfall_id = r.get("scryfall_id")
        if not scryfall_id:
            continue
        price_cols = [_int_or_none(r.get(k)) for k in (
            "price_market", "price_market_foil",
            "price_cents", "price_cents_foil", "price_cents_etched",
            "price_cents_nm", "price_cents_nm_foil", "price_cents_nm_etched",
            "price_cents_lp_plus", "price_cents_lp_plus_foil", "price_cents_lp_plus_etched",
        )]
        if _max_cents(*price_cols) < min_cents:
            continue

        printing_id = ids.get(scryfall_id)
        if printing_id is None:
            cur = conn.execute(
                "INSERT INTO printings (scryfall_id, name, set_code, collector_number) VALUES (?, ?, ?, ?)",
                (scryfall_id, r.get("name"), str(r.get("set_code", "")).upper(), r.get("number")),
            )
            printing_id = ids[scryfall_id] = cur.lastrowid

        daily_rows.append((printing_id, as_of, *price_cols, _int_or_none(r.get("available_quantity"))))
        n_daily += 1
        for v in r.get("variants") or []:
            for sale in v.get("recent_sales") or []:
                price = _int_or_none(sale.get("price"))
                if price is None or not sale.get("created_at"):
                    continue
                sales_rows.append((
                    printing_id, v.get("condition_id", ""), v.get("finish_id", ""),
                    v.get("language_id") or "EN", sale["created_at"], price,
                    _int_or_none(sale.get("quantity")) or 1,
                ))

        if len(daily_rows) >= _BATCH:
            _flush(conn, daily_sql, daily_rows)
        if len(sales_rows) >= _BATCH:
            _flush(conn, sales_sql, sales_rows)
    _flush(conn, daily_sql, daily_rows)
    _flush(conn, sales_sql, sales_rows)

    new_sales = conn.execute("SELECT COUNT(*) FROM manapool_sales").fetchone()[0] - sales_before
    _record_run(conn, "manapool", as_of, n_daily, new_sales)
    conn.commit()
    log.info("ManaPool %s: %d daily rows archived, %d new sales", as_of, n_daily, new_sales)
    return n_daily, new_sales


def ingest_manapool_catalog_file(conn: sqlite3.Connection, path: Path, min_price_usd: float = 0.5,
                                 force: bool = False) -> Optional[tuple[int, int]]:
    """Stream-ingest a cached catalog file, dated by its download time (file mtime, UTC)."""
    import ijson  # type: ignore[import-untyped]

    as_of = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc).date().isoformat()
    if not force and already_ingested(conn, "manapool", as_of):
        log.info("ManaPool catalog for %s already archived — skipping", as_of)
        return None
    with gzip.open(path, "rb") as f:
        return ingest_manapool_records(conn, ijson.items(f, "data.item"), as_of, min_price_usd, force=True)


def history_summary(conn: sqlite3.Connection) -> list[dict]:
    """Per-source archive coverage: days ingested, date range, latest row counts."""
    rows = conn.execute(
        """SELECT source, COUNT(*) AS days, MIN(as_of) AS first, MAX(as_of) AS last,
                  SUM(daily_rows) AS daily_rows, SUM(new_sales) AS sales
           FROM ingest_runs GROUP BY source ORDER BY source"""
    ).fetchall()
    return [dict(r) for r in rows]


# ── Nightly entry point ──────────────────────────────────────────────────────

def run_history_ingest(config: Any, sources: tuple[str, ...] = ("scryfall", "manapool"),
                       force: bool = False) -> None:
    """Download today's snapshots (if newer) and archive them. Each source runs independently."""
    from manabot.api.manapool_catalog import refresh_catalog
    from manabot.api.scryfall_bulk import download_default_cards

    min_price = config.history_min_price_usd
    with open_history_db(config.history_db_path) as conn:
        if "scryfall" in sources:
            try:
                download_default_cards(config.scryfall_default_cards_path)
                ingest_scryfall_file(conn, config.scryfall_default_cards_path, min_price, force)
            except Exception:
                conn.rollback()
                log.exception("Scryfall history ingest failed")
        if "manapool" in sources:
            try:
                refresh_catalog(config.catalog_cache_path)
                ingest_manapool_catalog_file(conn, config.catalog_cache_path, min_price, force)
            except Exception:
                conn.rollback()
                log.exception("ManaPool history ingest failed")
