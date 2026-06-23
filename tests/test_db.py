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
