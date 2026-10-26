import gzip
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from manabot.forecast.history import (
    history_summary,
    ingest_manapool_catalog_file,
    ingest_manapool_records,
    ingest_scryfall_cards,
    ingest_scryfall_file,
    init_history_db,
)


def _conn() -> sqlite3.Connection:
    return init_history_db(Path(":memory:"))


def _card(scryfall_id="sf-bolt", name="Lightning Bolt", usd="1.25", usd_foil=None, **extra) -> dict:
    card = {
        "id": scryfall_id,
        "oracle_id": "or-" + scryfall_id,
        "name": name,
        "set": "m10",
        "collector_number": "146",
        "set_type": "core",
        "rarity": "common",
        "released_at": "2009-07-17",
        "frame_effects": ["showcase"],
        "finishes": ["nonfoil", "foil"],
        "prices": {"usd": usd, "usd_foil": usd_foil, "usd_etched": None,
                   "eur": "1.10", "eur_foil": None, "tix": "0.03"},
        "edhrec_rank": 12,
    }
    card.update(extra)
    return card


def _record(scryfall_id="sf-bolt", market=125, sales=None, **extra) -> dict:
    record = {
        "scryfall_id": scryfall_id,
        "name": "Lightning Bolt",
        "set_code": "M10",
        "number": "146",
        "price_market": market,
        "price_market_foil": None,
        "price_cents": 99,
        "price_cents_nm": 110,
        "price_cents_lp_plus": 105,
        "available_quantity": 40,
        "variants": [
            {"condition_id": "NM", "finish_id": "NF", "language_id": "EN",
             "recent_sales": sales if sales is not None else [
                 {"created_at": "2026-10-01T10:00:00+00:00", "price": 120, "quantity": 1},
             ]},
        ],
    }
    record.update(extra)
    return record


# ── Scryfall ─────────────────────────────────────────────────────────────────

def test_scryfall_ingest_writes_printing_and_cents_prices():
    conn = _conn()
    assert ingest_scryfall_cards(conn, [_card()], "2026-10-26") == 1

    p = conn.execute("SELECT * FROM printings").fetchone()
    assert (p["scryfall_id"], p["oracle_id"], p["set_code"], p["frame_effects"]) == (
        "sf-bolt", "or-sf-bolt", "M10", "showcase")
    d = conn.execute("SELECT * FROM scryfall_daily").fetchone()
    assert (d["printing_id"], d["as_of"], d["usd_cents"], d["eur_cents"], d["tix_cents"], d["edhrec_rank"]) == (
        p["id"], "2026-10-26", 125, 110, 3, 12)


def test_scryfall_ingest_skips_prices_below_floor_but_keeps_metadata():
    conn = _conn()
    cards = [
        _card("sf-bulk", "Bulk Common", usd="0.10"),
        _card("sf-foil", "Foil Only Pricey", usd="0.10", usd_foil="2.00"),  # any finish counts
        _card("sf-none", "Unpriced", usd=None),
    ]
    assert ingest_scryfall_cards(conn, cards, "2026-10-26", min_price_usd=0.5) == 1

    assert conn.execute("SELECT COUNT(*) FROM printings").fetchone()[0] == 3
    names = [r[0] for r in conn.execute(
        "SELECT p.name FROM scryfall_daily d JOIN printings p ON p.id = d.printing_id")]
    assert names == ["Foil Only Pricey"]


def test_scryfall_ingest_reads_oracle_id_from_faces_for_reversible_cards():
    conn = _conn()
    card = _card(oracle_id=None, card_faces=[{"oracle_id": "or-face"}, {"oracle_id": "or-face"}])
    ingest_scryfall_cards(conn, [card], "2026-10-26")
    assert conn.execute("SELECT oracle_id FROM printings").fetchone()[0] == "or-face"


def test_scryfall_ingest_same_day_is_skipped_unless_forced():
    conn = _conn()
    ingest_scryfall_cards(conn, [_card(usd="1.00")], "2026-10-26")
    assert ingest_scryfall_cards(conn, [_card(usd="9.00")], "2026-10-26") is None
    assert conn.execute("SELECT usd_cents FROM scryfall_daily").fetchone()[0] == 100

    assert ingest_scryfall_cards(conn, [_card(usd="9.00")], "2026-10-26", force=True) == 1
    assert conn.execute("SELECT usd_cents FROM scryfall_daily").fetchone()[0] == 900


def test_scryfall_ingest_new_day_adds_row_and_updates_metadata():
    conn = _conn()
    ingest_scryfall_cards(conn, [_card(usd="1.00", reprint=False)], "2026-10-25")
    ingest_scryfall_cards(conn, [_card(usd="1.50", reprint=True)], "2026-10-26")

    assert [tuple(r) for r in conn.execute("SELECT as_of, usd_cents FROM scryfall_daily ORDER BY as_of")] == [
        ("2026-10-25", 100), ("2026-10-26", 150)]
    assert conn.execute("SELECT COUNT(*), MAX(reprint) FROM printings").fetchone()[:] == (1, 1)


def test_scryfall_file_is_dated_by_meta_updated_at(tmp_path):
    path = tmp_path / "default_cards.jsonl.gz"
    path.write_bytes(gzip.compress((json.dumps(_card()) + "\n").encode()))
    (tmp_path / "default_cards.meta.json").write_text(
        json.dumps({"updated_at": "2026-10-26T09:05:45+00:00"}), encoding="utf-8")

    conn = _conn()
    assert ingest_scryfall_file(conn, path) == 1
    assert conn.execute("SELECT as_of FROM scryfall_daily").fetchone()[0] == "2026-10-26"


# ── ManaPool ─────────────────────────────────────────────────────────────────

def test_manapool_ingest_links_to_scryfall_printing():
    conn = _conn()
    ingest_scryfall_cards(conn, [_card()], "2026-10-26")
    assert ingest_manapool_records(conn, [_record()], "2026-10-26") == (1, 1)

    d = conn.execute("SELECT * FROM manapool_daily").fetchone()
    pid = conn.execute("SELECT id FROM printings WHERE scryfall_id = 'sf-bolt'").fetchone()[0]
    assert (d["printing_id"], d["market_cents"], d["low_cents"], d["nm_cents"],
            d["lp_plus_cents"], d["available_quantity"]) == (pid, 125, 99, 110, 105, 40)
    s = conn.execute("SELECT * FROM manapool_sales").fetchone()
    assert (s["printing_id"], s["condition_id"], s["finish_id"], s["price_cents"], s["quantity"]) == (
        pid, "NM", "NF", 120, 1)


def test_manapool_ingest_creates_stub_printing_when_scryfall_unknown():
    conn = _conn()
    ingest_manapool_records(conn, [_record("sf-new")], "2026-10-26")
    p = conn.execute("SELECT scryfall_id, name, set_code, collector_number, oracle_id FROM printings").fetchone()
    assert tuple(p) == ("sf-new", "Lightning Bolt", "M10", "146", None)

    # Scryfall arriving later fills in the stub instead of duplicating it.
    ingest_scryfall_cards(conn, [_card("sf-new")], "2026-10-27")
    assert conn.execute("SELECT COUNT(*), MAX(oracle_id) FROM printings").fetchone()[:] == (1, "or-sf-new")


def test_manapool_sales_are_deduplicated_across_days():
    conn = _conn()
    day1 = [{"created_at": "2026-10-01T10:00:00+00:00", "price": 120, "quantity": 1}]
    day2 = day1 + [{"created_at": "2026-10-02T11:00:00+00:00", "price": 130, "quantity": 2}]
    assert ingest_manapool_records(conn, [_record(sales=day1)], "2026-10-01") == (1, 1)
    assert ingest_manapool_records(conn, [_record(sales=day2)], "2026-10-02") == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM manapool_sales").fetchone()[0] == 2


def test_manapool_ingest_skips_records_below_floor():
    conn = _conn()
    cheap = _record("sf-cheap", market=20, price_cents=10, price_cents_nm=12, price_cents_lp_plus=11)
    assert ingest_manapool_records(conn, [cheap, _record()], "2026-10-26", min_price_usd=0.5) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM printings").fetchone()[0] == 1


def test_manapool_catalog_file_is_dated_by_download_time_and_skips_repeat(tmp_path):
    path = tmp_path / "catalog.json.gz"
    path.write_bytes(gzip.compress(json.dumps({"data": [_record()]}).encode()))
    stamp = datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))

    conn = _conn()
    assert ingest_manapool_catalog_file(conn, path) == (1, 1)
    assert conn.execute("SELECT as_of FROM manapool_daily").fetchone()[0] == "2026-10-26"
    assert ingest_manapool_catalog_file(conn, path) is None


def test_history_summary_reports_each_source():
    conn = _conn()
    ingest_scryfall_cards(conn, [_card()], "2026-10-25")
    ingest_scryfall_cards(conn, [_card()], "2026-10-26")
    ingest_manapool_records(conn, [_record()], "2026-10-26")

    summary = {r["source"]: r for r in history_summary(conn)}
    assert (summary["scryfall"]["days"], summary["scryfall"]["first"], summary["scryfall"]["last"]) == (
        2, "2026-10-25", "2026-10-26")
    assert (summary["manapool"]["daily_rows"], summary["manapool"]["sales"]) == (1, 1)
