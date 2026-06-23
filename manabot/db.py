from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from manabot.models import Condition, Finish, PriceListing

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
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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
