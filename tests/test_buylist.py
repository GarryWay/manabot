import textwrap
from pathlib import Path

import pytest

from manabot.buylist import BuyListError, load_buylist
from manabot.models import Condition, Finish


FIXTURE = Path(__file__).parent / "fixtures" / "sample_buylist.csv"


def test_load_valid_fixture():
    items = load_buylist(FIXTURE)
    assert len(items) == 4


def test_scryfall_id_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].scryfall_id == "e3285e6b-3e79-4d7c-bf96-d920f973b122"
    assert items[1].scryfall_id is None


def test_condition_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].min_condition == Condition.LP
    assert items[1].min_condition == Condition.MP
    assert items[2].min_condition == Condition.NM


def test_foil_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].foil == Finish.NONFOIL
    assert items[1].foil == Finish.ANY
    assert items[3].foil == Finish.FOIL


def test_allowed_sets_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].allowed_sets == []
    assert items[2].allowed_sets == ["LEA"]


def test_in_universe_only_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].in_universe_only is False
    assert items[2].in_universe_only is True


def test_tags_parsed():
    items = load_buylist(FIXTURE)
    assert items[0].tags == ["burn-deck"]
    assert items[1].tags == ["legacy", "customer:John"]


def test_extra_columns_ignored():
    # The fixture has a 'notes' column — should not raise
    items = load_buylist(FIXTURE)
    assert len(items) == 4


def test_missing_required_column(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text("card_name,target_quantity,max_price_usd\nLightning Bolt,4,1.50\n", encoding="utf-8")
    with pytest.raises(BuyListError, match="min_condition"):
        load_buylist(csv)


def test_non_numeric_price(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,4,expensive,NM\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="max_price_usd"):
        load_buylist(csv)


def test_invalid_condition(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,4,1.50,MINT\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="min_condition"):
        load_buylist(csv)


def test_negative_quantity(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "card_name,target_quantity,max_price_usd,min_condition\nLightning Bolt,-1,1.50,NM\n",
        encoding="utf-8",
    )
    with pytest.raises(BuyListError, match="target_quantity"):
        load_buylist(csv)


def test_excel_bom_encoding(tmp_path):
    csv = tmp_path / "bom.csv"
    csv.write_bytes(
        b"\xef\xbb\xbfcard_name,target_quantity,max_price_usd,min_condition\r\nLightning Bolt,4,1.50,NM\r\n"
    )
    items = load_buylist(csv)
    assert items[0].card_name == "Lightning Bolt"


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_buylist(Path("nonexistent.csv"))
