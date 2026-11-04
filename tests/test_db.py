from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manabot.db import (
    get_last_run,
    get_latest_price,
    get_price_history,
    init_db,
    insert_listings,
    record_fetch_run,
)
from manabot.models import Condition, Finish, PriceListing

SCRYFALL_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"


def make_listing(price: float, days_ago: int = 0, scryfall_id: str = SCRYFALL_ID) -> PriceListing:
    fetched_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return PriceListing(
        scryfall_id=scryfall_id,
        card_name="Lightning Bolt",
        set_code="LEB",
        condition=Condition.NM,
        finish=Finish.NONFOIL,
        price_usd=price,
        quantity_available=4,
        seller_id="seller1",
        fetched_at=fetched_at,
    )


@pytest.fixture
def conn():
    c = init_db(Path(":memory:"))
    yield c
    c.close()


def test_init_creates_tables(conn):
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "price_snapshots" in tables
    assert "fetch_runs" in tables


def test_insert_and_retrieve(conn):
    insert_listings(conn, [make_listing(1.50)])
    conn.commit()
    price = get_latest_price(conn, SCRYFALL_ID)
    assert price == pytest.approx(1.50)


def test_get_latest_price_returns_most_recent(conn):
    insert_listings(conn, [make_listing(2.00, days_ago=3), make_listing(1.50, days_ago=0)])
    conn.commit()
    assert get_latest_price(conn, SCRYFALL_ID) == pytest.approx(1.50)


def test_get_latest_price_missing(conn):
    assert get_latest_price(conn, "no-such-id") is None


def test_get_price_history(conn):
    for days_ago in [6, 3, 1]:
        insert_listings(conn, [make_listing(1.00 + days_ago * 0.10, days_ago=days_ago)])
    conn.commit()
    history = get_price_history(conn, SCRYFALL_ID, days=7)
    assert len(history) == 3
    # Oldest first
    assert history[0][1] > history[-1][1]


def test_get_price_history_empty(conn):
    assert get_price_history(conn, "no-such-id") == []


def test_record_fetch_run(conn):
    now = datetime.now(timezone.utc)
    record_fetch_run(conn, now, now, listings_fetched=100, matches_found=5)
    conn.commit()
    last = get_last_run(conn)
    assert last is not None


def test_get_last_run_empty(conn):
    assert get_last_run(conn) is None


def test_init_db_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    c1 = init_db(db_path)
    c1.close()
    c2 = init_db(db_path)  # should not raise
    c2.close()


def _sale(order_id: str = "ord-1", price: float = 5.32):
    from manabot.models import CompletedSale
    return CompletedSale(
        order_id=order_id, scryfall_id=SCRYFALL_ID, card_name="Lightning Bolt", set_code="LEB",
        condition=Condition.LP, finish=Finish.NONFOIL, quantity=1, sold_price_usd=price,
        sold_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_record_sales_skips_duplicates(conn):
    from manabot.db import get_last_sales_sync, record_sales

    assert record_sales(conn, [_sale()]) == 1
    assert record_sales(conn, [_sale(), _sale("ord-2")]) == 1
    assert conn.execute("SELECT COUNT(*) FROM sales_history").fetchone()[0] == 2
    assert get_last_sales_sync(conn) == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_init_db_drops_profit_tracking_from_old_schema(tmp_path):
    """Databases created before 2026-11-04 had cost_basis, price_floor_tracking and
    cost/margin columns on sales_history; init_db removes them and keeps the sales."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE cost_basis (id INTEGER PRIMARY KEY, scryfall_id TEXT, cost_usd REAL);
        CREATE INDEX idx_cost_basis_scryfall ON cost_basis (scryfall_id);
        CREATE TABLE price_floor_tracking (scryfall_id TEXT, condition TEXT, finish TEXT,
            below_floor_since TEXT, PRIMARY KEY (scryfall_id, condition, finish));
        CREATE TABLE sales_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, scryfall_id TEXT NOT NULL,
            card_name TEXT NOT NULL, set_code TEXT NOT NULL, condition TEXT NOT NULL,
            finish TEXT NOT NULL DEFAULT 'nonfoil', quantity INTEGER NOT NULL,
            sold_price_usd REAL NOT NULL, cost_usd REAL, margin_usd REAL, sold_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(order_id, scryfall_id, condition, finish));
        INSERT INTO sales_history (order_id, scryfall_id, card_name, set_code, condition,
            quantity, sold_price_usd, cost_usd, margin_usd, sold_at)
            VALUES ('ord-1', 'sf', 'Bolt', 'LEB', 'LP', 1, 5.32, 1.0, 4.32, '2026-11-01');
    """)
    old.commit()
    old.close()

    conn = init_db(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sales_history)")}
    sales = conn.execute("SELECT order_id, sold_price_usd FROM sales_history").fetchall()
    conn.close()
    init_db(path).close()  # second open is a no-op

    assert "cost_basis" not in tables and "price_floor_tracking" not in tables
    assert "cost_usd" not in cols and "margin_usd" not in cols
    assert [tuple(r) for r in sales] == [("ord-1", 5.32)]
