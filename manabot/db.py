from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS cost_basis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT NOT NULL,
    card_name   TEXT NOT NULL,
    set_code    TEXT NOT NULL,
    condition   TEXT NOT NULL,
    finish      TEXT NOT NULL DEFAULT 'nonfoil',
    cost_usd    REAL NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    acquired_at TEXT NOT NULL,
    source      TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_basis_scryfall
    ON cost_basis (scryfall_id, condition, finish);

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
    cost_usd       REAL,
    margin_usd     REAL,
    sold_at        TEXT NOT NULL,
    recorded_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, scryfall_id, condition, finish)
);

CREATE TABLE IF NOT EXISTS price_floor_tracking (
    scryfall_id       TEXT NOT NULL,
    condition         TEXT NOT NULL,
    finish            TEXT NOT NULL DEFAULT 'nonfoil',
    below_floor_since TEXT,
    PRIMARY KEY (scryfall_id, condition, finish)
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


def get_cost_basis(
    conn: sqlite3.Connection,
    scryfall_id: str,
    condition: Condition,
    finish: Finish,
) -> Optional[dict]:
    """Return the most recent cost basis record for a listing, or None."""
    finish_val = Finish.NONFOIL.value if finish == Finish.ANY else finish.value
    row = conn.execute(
        """SELECT cost_usd, quantity, acquired_at, source
           FROM cost_basis
           WHERE scryfall_id = ? AND condition = ? AND finish = ?
           ORDER BY acquired_at DESC LIMIT 1""",
        (scryfall_id, condition.value, finish_val),
    ).fetchone()
    return dict(row) if row else None


def set_cost_basis(
    conn: sqlite3.Connection,
    scryfall_id: str,
    card_name: str,
    set_code: str,
    condition: Condition,
    finish: Finish,
    cost_usd: float,
    quantity: int,
    acquired_at: datetime,
    source: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Insert a cost basis record."""
    finish_val = Finish.NONFOIL.value if finish == Finish.ANY else finish.value
    conn.execute(
        """INSERT INTO cost_basis
           (scryfall_id, card_name, set_code, condition, finish,
            cost_usd, quantity, acquired_at, source, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scryfall_id, card_name, set_code, condition.value, finish_val,
         cost_usd, quantity, acquired_at.isoformat(), source, notes),
    )


def get_days_below_floor(
    conn: sqlite3.Connection,
    scryfall_id: str,
    condition: Condition,
    finish: Finish,
) -> int:
    """Number of days this listing has continuously been below the cost floor."""
    finish_val = Finish.NONFOIL.value if finish == Finish.ANY else finish.value
    row = conn.execute(
        """SELECT below_floor_since FROM price_floor_tracking
           WHERE scryfall_id = ? AND condition = ? AND finish = ?""",
        (scryfall_id, condition.value, finish_val),
    ).fetchone()
    if not row or not row["below_floor_since"]:
        return 0
    try:
        since = datetime.fromisoformat(row["below_floor_since"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - since).days)
    except (ValueError, TypeError):
        return 0


def update_floor_tracking(
    conn: sqlite3.Connection,
    scryfall_id: str,
    condition: Condition,
    finish: Finish,
    new_price_usd: float,
    cost_floor_usd: Optional[float],
) -> None:
    """Set or clear the below_floor_since timestamp based on new price vs cost floor."""
    finish_val = Finish.NONFOIL.value if finish == Finish.ANY else finish.value
    if cost_floor_usd is not None and new_price_usd < cost_floor_usd:
        conn.execute(
            """INSERT INTO price_floor_tracking (scryfall_id, condition, finish, below_floor_since)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(scryfall_id, condition, finish)
               DO UPDATE SET below_floor_since = COALESCE(below_floor_since, excluded.below_floor_since)""",
            (scryfall_id, condition.value, finish_val, datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute(
            """INSERT INTO price_floor_tracking (scryfall_id, condition, finish, below_floor_since)
               VALUES (?, ?, ?, NULL)
               ON CONFLICT(scryfall_id, condition, finish)
               DO UPDATE SET below_floor_since = NULL""",
            (scryfall_id, condition.value, finish_val),
        )


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
        cost_row = conn.execute(
            """SELECT cost_usd FROM cost_basis
               WHERE scryfall_id = ? AND condition = ? AND finish = ?
               ORDER BY acquired_at DESC LIMIT 1""",
            (sale.scryfall_id, sale.condition.value, finish_val),
        ).fetchone()
        cost_usd = cost_row["cost_usd"] if cost_row else None
        margin_usd = round(sale.sold_price_usd - cost_usd, 4) if cost_usd is not None else None
        cursor = conn.execute(
            """INSERT OR IGNORE INTO sales_history
               (order_id, scryfall_id, card_name, set_code, condition, finish,
                quantity, sold_price_usd, cost_usd, margin_usd, sold_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sale.order_id, sale.scryfall_id, sale.card_name, sale.set_code,
             sale.condition.value, finish_val, sale.quantity,
             sale.sold_price_usd, cost_usd, margin_usd, sale.sold_at.isoformat()),
        )
        inserted += cursor.rowcount
    return inserted


def get_margin_report(
    conn: sqlite3.Connection,
    days: Optional[int] = None,
    card_name_filter: Optional[str] = None,
) -> list[dict]:
    """Return P&L grouped by card_name, sorted by total_margin descending."""
    where: list[str] = []
    params: list = []
    if days:
        where.append(f"sold_at >= DATETIME('now', '-{days} days')")
    if card_name_filter:
        where.append("card_name LIKE ?")
        params.append(f"%{card_name_filter}%")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT
                card_name,
                SUM(quantity) AS total_sold,
                ROUND(AVG(sold_price_usd), 2) AS avg_sell_price,
                ROUND(AVG(cost_usd), 2) AS avg_cost,
                ROUND(SUM(margin_usd), 2) AS total_margin,
                COUNT(*) AS sale_count,
                SUM(CASE WHEN margin_usd > 0 THEN 1 ELSE 0 END) AS profitable_count
            FROM sales_history
            {where_clause}
            GROUP BY card_name
            ORDER BY total_margin DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
