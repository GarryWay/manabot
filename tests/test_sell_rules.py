"""Tests for per-card sell rules (manabot/sell_rules.py)."""
from __future__ import annotations

from manabot.models import Condition, Finish, SellerListing
from manabot.sell_rules import (
    SellRule, find_rule, load_sell_rules, read_rows, resolve_row, row_from_listing, write_rows,
)


def _listing(**kw) -> SellerListing:
    base = dict(
        inventory_id="inv-1", product_id="prod-1", scryfall_id="sf-1", card_name="Simulacrum Shaper",
        set_code="TST", condition=Condition.NM, finish=Finish.NONFOIL, language="EN",
        quantity=1, price_usd=300.0, number="42", mtgjson_id="mj-1",
    )
    base.update(kw)
    return SellerListing(**base)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


HEADER = "card_name,set_code,collector_number,scryfall_id,mtgjson_id,condition,finish,strategy,min_price_usd,max_price_usd,notes\n"


def test_missing_file_means_no_rules(tmp_path):
    assert load_sell_rules(tmp_path / "nope.csv") == []


def test_load_parses_fields(tmp_path):
    p = _write(tmp_path / "r.csv", HEADER + "Simulacrum Shaper,tst,42,sf-1,,nm,Foil,Hold,$250,300.5,keep\n")
    [r] = load_sell_rules(p)
    assert r.set_code == "TST"
    assert r.condition == Condition.NM
    assert r.finish == Finish.FOIL
    assert r.strategy == "hold"
    assert r.min_price_usd == 250.0 and r.max_price_usd == 300.5
    assert r.notes == "keep"


def test_load_defaults_strategy_and_blank_bounds(tmp_path):
    p = _write(tmp_path / "r.csv", HEADER + "X,TST,1,sf-1,,,,,,,\n")
    [r] = load_sell_rules(p)
    assert r.strategy == "balanced"
    assert r.min_price_usd is None and r.max_price_usd is None
    assert r.condition is None and r.finish is None


def test_load_skips_bad_rows(tmp_path):
    p = _write(tmp_path / "r.csv", HEADER
               + "BadStrategy,TST,1,sf-1,,,,yolo,,,\n"
               + "Inverted,TST,1,sf-1,,,,,10,5,\n"
               + "NoId,TST,1,,,,,,,,\n"
               + "BadCond,TST,1,sf-1,,XX,,,,,\n"
               + "Good,TST,1,sf-1,,,,,,,\n")
    assert [r.card_name for r in load_sell_rules(p)] == ["Good"]


def test_load_handles_excel_bom_and_extra_columns(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text(HEADER.strip() + ",extra\nX,TST,1,sf-1,,,,aggressive,,,,whatever\n", encoding="utf-8-sig")
    [r] = load_sell_rules(p)
    assert r.strategy == "aggressive"


def test_match_by_scryfall_id_and_optional_condition_finish():
    lst = _listing()
    assert SellRule(scryfall_id="sf-1").matches(lst)
    assert SellRule(scryfall_id="sf-1", condition=Condition.NM, finish=Finish.NONFOIL).matches(lst)
    assert not SellRule(scryfall_id="sf-1", condition=Condition.LP).matches(lst)
    assert not SellRule(scryfall_id="sf-1", finish=Finish.FOIL).matches(lst)
    assert not SellRule(scryfall_id="sf-2").matches(lst)


def test_mtgjson_id_takes_precedence_for_double_sided_pairings():
    """Two double-sided token pairings share the front face's scryfall_id."""
    a, b = _listing(mtgjson_id="mj-a"), _listing(mtgjson_id="mj-b")
    rule = SellRule(scryfall_id="sf-1", mtgjson_id="mj-a")
    assert rule.matches(a) and not rule.matches(b)


def test_find_rule_prefers_most_specific():
    generic = SellRule(scryfall_id="sf-1", strategy="balanced")
    specific = SellRule(scryfall_id="sf-1", condition=Condition.NM, strategy="hold")
    assert find_rule([generic, specific], _listing()) is specific
    assert find_rule([generic, specific], _listing(condition=Condition.LP)) is generic
    assert find_rule([], _listing()) is None


class _FakeScryfall:
    def __init__(self, ids=None, names=None):
        self.ids, self.names = ids or {}, names or {}

    def lookup_by_set_number(self, set_code, number):
        return self.ids.get((set_code.lower(), number))

    def get_card_metadata(self, sid):
        return {"name": self.names.get(sid, "")}


def test_resolve_prefers_our_inventory():
    row = {"set_code": "tst", "collector_number": "42"}
    assert resolve_row(row, [_listing()], _FakeScryfall()) == "resolved"
    assert row["scryfall_id"] == "sf-1" and row["mtgjson_id"] == "mj-1"
    assert row["card_name"] == "Simulacrum Shaper" and row["set_code"] == "TST"


def test_resolve_falls_back_to_scryfall_and_corrects_name():
    sf = _FakeScryfall(ids={("m10", "146"): "bolt-sf"}, names={"bolt-sf": "Lightning Bolt"})
    row = {"card_name": "lightnin bolt", "set_code": "M10", "collector_number": "146"}
    assert resolve_row(row, [], sf) == "resolved"
    assert row["scryfall_id"] == "bolt-sf"
    assert row["card_name"] == "Lightning Bolt"


def test_resolve_compound_number_requires_inventory():
    row = {"set_code": "TST", "collector_number": "2-7"}
    assert resolve_row(row, [], _FakeScryfall()) == "unresolved"


def test_resolve_leaves_rows_with_ids_alone():
    row = {"scryfall_id": "already", "set_code": "TST", "collector_number": "42"}
    assert resolve_row(row, [_listing()], _FakeScryfall()) == "already"
    assert row["scryfall_id"] == "already"


def test_resolve_unresolved_without_set_or_number():
    assert resolve_row({"card_name": "X"}, [], _FakeScryfall()) == "unresolved"


def test_write_then_load_round_trip(tmp_path):
    p = tmp_path / "sub" / "rules.csv"
    row = row_from_listing(_listing(), strategy="hold")
    row["min_price_usd"] = "250.00"
    write_rows(p, [row])
    assert read_rows(p)[0]["scryfall_id"] == "sf-1"
    [r] = load_sell_rules(p)
    assert r.strategy == "hold" and r.min_price_usd == 250.0
    assert r.matches(_listing())
