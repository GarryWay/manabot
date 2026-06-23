from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manabot.analyzer import analyze, summarize
from manabot.db import init_db, insert_listings
from manabot.models import (
    BuyListItem,
    Condition,
    Finish,
    MatchResult,
    MatchStatus,
    PriceListing,
    TrendDirection,
)

NOW = datetime.now(timezone.utc)
BOLT_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"


def make_listing(price: float, days_ago: int = 0) -> PriceListing:
    return PriceListing(
        scryfall_id=BOLT_ID,
        card_name="Lightning Bolt",
        set_code="LEB",
        condition=Condition.NM,
        finish=Finish.NONFOIL,
        price_usd=price,
        quantity_available=4,
        seller_id="seller1",
        fetched_at=NOW - timedelta(days=days_ago),
    )


def make_result(best_price: float, scryfall_id: str = BOLT_ID) -> MatchResult:
    item = BuyListItem(
        card_name="Lightning Bolt",
        scryfall_id=scryfall_id,
        target_quantity=4,
        max_price_usd=2.00,
        min_condition=Condition.LP,
    )
    listing = make_listing(best_price)
    return MatchResult(
        buy_list_item=item,
        listings=[listing],
        best_price=best_price,
        is_good_buy=best_price <= 2.00,
        status=MatchStatus.MATCHED,
    )


@pytest.fixture
def conn():
    c = init_db(Path(":memory:"))
    yield c
    c.close()


def test_trend_new_when_no_history(conn):
    results = analyze([make_result(1.50)], conn)
    assert results[0].trend is not None
    assert results[0].trend.direction == TrendDirection.NEW


def test_trend_down_when_price_dropped(conn):
    insert_listings(conn, [make_listing(2.00, days_ago=6)])
    conn.commit()
    results = analyze([make_result(1.50)], conn)
    assert results[0].trend.direction == TrendDirection.DOWN


def test_trend_up_when_price_rose(conn):
    insert_listings(conn, [make_listing(1.00, days_ago=6)])
    conn.commit()
    results = analyze([make_result(1.50)], conn)
    assert results[0].trend.direction == TrendDirection.UP


def test_trend_flat_within_threshold(conn):
    insert_listings(conn, [make_listing(1.50, days_ago=6)])
    conn.commit()
    results = analyze([make_result(1.52)], conn, trend_threshold_pct=5.0)
    assert results[0].trend.direction == TrendDirection.FLAT


def test_trend_boundary_exactly_at_threshold(conn):
    # 4.9% change — should be FLAT (below threshold)
    insert_listings(conn, [make_listing(1.00, days_ago=6)])
    conn.commit()
    results = analyze([make_result(1.049)], conn, trend_threshold_pct=5.0)
    assert results[0].trend.direction == TrendDirection.FLAT


def test_trend_just_over_threshold(conn):
    insert_listings(conn, [make_listing(1.00, days_ago=6)])
    conn.commit()
    results = analyze([make_result(1.051)], conn, trend_threshold_pct=5.0)
    assert results[0].trend.direction == TrendDirection.UP


def test_unresolved_items_skipped(conn):
    item = BuyListItem(
        card_name="Nonexistent Card",
        scryfall_id=None,
        target_quantity=1,
        max_price_usd=1.00,
        min_condition=Condition.NM,
    )
    result = MatchResult(buy_list_item=item, status=MatchStatus.UNRESOLVED)
    results = analyze([result], conn)
    assert results[0].trend is None


def test_summarize():
    r1 = make_result(1.00)
    r1.is_good_buy = True
    r2 = make_result(5.00)
    r2.is_good_buy = False
    r3 = MatchResult(
        buy_list_item=r1.buy_list_item,
        status=MatchStatus.UNRESOLVED,
    )
    summary = summarize([r1, r2, r3])
    assert summary["total_checked"] == 3
    assert summary["good_buy_count"] == 1
    assert summary["unresolved_count"] == 1
