from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from manabot.models import Condition, Finish, PriceListing, CompletedSale

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id      TEXT    NOT NULL,
    card_name        TEXT,
    set_code         TEXT,
    condition        TEXT,
    finish           TEXT,
    price_usd        REAL    NOT NULL,
    quantity_available INTEGER,
    seller_id        TEXT,
    fetched_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_scryfall_fetched
    ON price_snapshots (scryfall_id, fetched_at);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT    NOT NULL,
    completed_at     TEXT,
    listings_fetched INTEGER,
    matches_found    INTEGER
);

CREATE TABLE IF NOT EXISTS price_updates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id      TEXT NOT NULL,
    card_name        TEXT NOT NULL,
    set_code         TEXT NOT NULL,
    condition        TEXT NOT NULL,
    finish           TEXT NOT NULL DEFAULT 'nonfoil',
    old_price_usd    REAL NOT NULL,
    new_price_usd    REAL NOT NULL,
    market_price_usd REAL,
    list_floor_usd   REAL,
    reason           TEXT NOT NULL,
    dry_run          INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sales_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT NOT NULL,
    scryfall_id    TEXT NOT NULL,
    card_name      TEXT NOT NULL,
    set_code       TEXT NOT NULL,
    condition      TEXT NOT NULL,
    finish         TEXT NOT NULL DEFAULT 'nonfoil',
    quantity       INTEGER NOT NULL,
    sold_price_usd REAL NOT NULL,
    sold_at        TEXT NOT NULL,
    recorded_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, scryfall_id, condition, finish)
);
"""


def _drop_profit_tracking(conn: sqlite3.Connection) -> None:
    """Remove the cost-basis / margin / cost-floor schema from databases created
    before it was dropped (2026-11-04). Sales are tracked for realized prices only."""
    conn.execute("DROP TABLE IF EXISTS cost_basis")
    conn.execute("DROP TABLE IF EXISTS price_floor_tracking")
    sales_cols = {row[1] for row in conn.execute("PRAGMA table_info(sales_history)")}
    for col in ("cost_usd", "margin_usd"):
        if col in sales_cols:
            try:
                conn.execute(f"ALTER TABLE sales_history DROP COLUMN {col}")
            except sqlite3.OperationalError:
                # DROP COLUMN needs SQLite >= 3.35. The leftover column is nullable
                # and no longer written, so it's harmless; never fail a DB open on it.
                return


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _drop_profit_tracking(conn)
    conn.commit()
    return conn


@contextmanager
def open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_listings(conn: sqlite3.Connection, listings: list[PriceListing]) -> None:
    rows = [
        (
            l.scryfall_id,
            l.card_name,
            l.set_code,
            l.condition.value,
            l.finish.value,
            l.price_usd,
            l.quantity_available,
            l.seller_id,
            l.fetched_at.isoformat(),
        )
        for l in listings
    ]
    conn.executemany(
        """INSERT INTO price_snapshots
           (scryfall_id, card_name, set_code, condition, finish,
            price_usd, quantity_available, seller_id, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )


def get_latest_price(conn: sqlite3.Connection, scryfall_id: str) -> Optional[float]:
    row = conn.execute(
        "SELECT price_usd FROM price_snapshots WHERE scryfall_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (scryfall_id,),
    ).fetchone()
    return row["price_usd"] if row else None


def get_price_history(
    conn: sqlite3.Connection, scryfall_id: str, days: int = 7
) -> list[tuple[datetime, float]]:
    """Return (fetched_at, min_price_usd) per day for the past `days` days, oldest first."""
    rows = conn.execute(
        """
        SELECT DATE(fetched_at) AS day, MIN(price_usd) AS min_price
        FROM price_snapshots
        WHERE scryfall_id = ?
          AND fetched_at >= DATETIME('now', ? || ' days')
        GROUP BY day
        ORDER BY day ASC
        """,
        (scryfall_id, f"-{days}"),
    ).fetchall()
    return [(datetime.fromisoformat(r["day"]), r["min_price"]) for r in rows]


def record_fetch_run(
    conn: sqlite3.Connection,
    started_at: datetime,
    completed_at: datetime,
    listings_fetched: int,
    matches_found: int,
) -> int:
    cursor = conn.execute(
        """INSERT INTO fetch_runs (started_at, completed_at, listings_fetched, matches_found)
           VALUES (?, ?, ?, ?)""",
        (started_at.isoformat(), completed_at.isoformat(), listings_fetched, matches_found),
    )
    return cursor.lastrowid


def get_last_run(conn: sqlite3.Connection) -> Optional[datetime]:
    row = conn.execute("SELECT MAX(completed_at) AS ts FROM fetch_runs").fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def log_price_update(
    conn: sqlite3.Connection,
    scryfall_id: str,
    card_name: str,
    set_code: str,
    condition: Condition,
    finish: Finish,
    old_price_usd: float,
    new_price_usd: float,
    market_price_usd: Optional[float],
    list_floor_usd: Optional[float],
    reason: str,
    dry_run: bool = False,
) -> None:
    """Write a price update record to the audit log."""
    finish_val = Finish.NONFOIL.value if finish == Finish.ANY else finish.value
    conn.execute(
        """INSERT INTO price_updates
           (scryfall_id, card_name, set_code, condition, finish,
            old_price_usd, new_price_usd, market_price_usd, list_floor_usd, reason, dry_run)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scryfall_id, card_name, set_code, condition.value, finish_val,
         old_price_usd, new_price_usd, market_price_usd, list_floor_usd, reason, 1 if dry_run else 0),
    )


def get_last_sales_sync(conn: sqlite3.Connection) -> Optional[datetime]:
    """Return the sold_at timestamp of the most recent recorded sale."""
    row = conn.execute("SELECT MAX(sold_at) AS last FROM sales_history").fetchone()
    if row and row["last"]:
        try:
            return datetime.fromisoformat(row["last"])
        except (ValueError, TypeError):
            return None
    return None


def record_sales(conn: sqlite3.Connection, sales: list[CompletedSale]) -> int:
    """Insert completed sales; skips duplicates. Returns count inserted."""
    inserted = 0
    for sale in sales:
        finish_val = Finish.NONFOIL.value if sale.finish == Finish.ANY else sale.finish.value
        cursor = conn.execute(
            """INSERT OR IGNORE INTO sales_history
               (order_id, scryfall_id, card_name, set_code, condition, finish,
                quantity, sold_price_usd, sold_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sale.order_id, sale.scryfall_id, sale.card_name, sale.set_code,
             sale.condition.value, finish_val, sale.quantity,
             sale.sold_price_usd, sale.sold_at.isoformat()),
        )
        inserted += cursor.rowcount
    return inserted
