import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import responses as resp_lib

from manabot.api.scryfall_bulk import ScryfallBulk, download_oracle_cards

_BULK_META_URL = "https://api.scryfall.com/bulk-data"
_DOWNLOAD_URL = "https://data.scryfall.io/oracle-cards/oracle-cards-20260101.json"

_BULK_RESPONSE = {
    "data": [
        {
            "type": "oracle_cards",
            "updated_at": "2026-01-01T12:00:00+00:00",
            "download_uri": _DOWNLOAD_URL,
            "size": 1024,
        }
    ]
}

_CARDS_PAYLOAD = json.dumps([
    {
        "id": "abc",
        "name": "Lightning Bolt",
        "prices": {"usd": "1.25", "usd_foil": "3.00"},
        "legalities": {"vintage": "legal", "legacy": "legal", "modern": "legal",
                       "commander": "legal", "pioneer": "not_legal", "standard": "not_legal"},
    }
]).encode()


@resp_lib.activate
def test_download_oracle_cards_new_file():
    resp_lib.add(resp_lib.GET, _BULK_META_URL, json=_BULK_RESPONSE)
    resp_lib.add(resp_lib.GET, _DOWNLOAD_URL, body=_CARDS_PAYLOAD, content_type="application/json")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "oracle.json"
        result = download_oracle_cards(path)

    assert result is True


@resp_lib.activate
def test_download_oracle_cards_writes_data():
    resp_lib.add(resp_lib.GET, _BULK_META_URL, json=_BULK_RESPONSE)
    resp_lib.add(resp_lib.GET, _DOWNLOAD_URL, body=_CARDS_PAYLOAD, content_type="application/json")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "oracle.json"
        download_oracle_cards(path)
        cards = json.loads(path.read_text(encoding="utf-8"))

    assert cards[0]["name"] == "Lightning Bolt"


@resp_lib.activate
def test_download_oracle_cards_writes_meta_sidecar():
    resp_lib.add(resp_lib.GET, _BULK_META_URL, json=_BULK_RESPONSE)
    resp_lib.add(resp_lib.GET, _DOWNLOAD_URL, body=_CARDS_PAYLOAD, content_type="application/json")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "oracle.json"
        download_oracle_cards(path)
        meta = json.loads((path.with_name("oracle.meta.json")).read_text(encoding="utf-8"))

    assert meta["updated_at"] == "2026-01-01T12:00:00+00:00"


@resp_lib.activate
def test_download_oracle_cards_skips_when_current():
    """Should not re-download when the stored updated_at matches Scryfall's."""
    resp_lib.add(resp_lib.GET, _BULK_META_URL, json=_BULK_RESPONSE)

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "oracle.json"
        path.write_bytes(_CARDS_PAYLOAD)
        meta_path = path.with_name("oracle.meta.json")
        meta_path.write_text(
            json.dumps({"updated_at": "2026-01-01T12:00:00+00:00"}),
            encoding="utf-8",
        )
        result = download_oracle_cards(path)

    assert result is False
    assert len(resp_lib.calls) == 1  # only the metadata request, no download


@resp_lib.activate
def test_download_oracle_cards_force_re_downloads():
    """force=True should re-download even if updated_at matches."""
    resp_lib.add(resp_lib.GET, _BULK_META_URL, json=_BULK_RESPONSE)
    resp_lib.add(resp_lib.GET, _DOWNLOAD_URL, body=_CARDS_PAYLOAD, content_type="application/json")

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "oracle.json"
        path.write_bytes(_CARDS_PAYLOAD)
        meta_path = path.with_name("oracle.meta.json")
        meta_path.write_text(
            json.dumps({"updated_at": "2026-01-01T12:00:00+00:00"}),
            encoding="utf-8",
        )
        result = download_oracle_cards(path, force=True)

    assert result is True


def _write_bulk(cards: list[dict], tmp_dir: str) -> Path:
    path = Path(tmp_dir) / "oracle.json"
    path.write_text(json.dumps(cards), encoding="utf-8")
    return path


_BOLT = {
    "id": "abc",
    "name": "Lightning Bolt",
    "prices": {"usd": "1.25", "usd_foil": "3.00"},
    "legalities": {"vintage": "legal", "legacy": "legal", "modern": "legal",
                   "commander": "legal", "pioneer": "not_legal", "standard": "not_legal"},
}

_PLANEQUAKE = {
    "id": "def",
    "name": "Planequake",
    "prices": {"usd": "11.99", "usd_foil": None},
    "legalities": {"vintage": "not_legal", "legacy": "not_legal", "modern": "not_legal",
                   "commander": "not_legal", "pioneer": "not_legal", "standard": "not_legal"},
}

_DFC = {
    "id": "ghi",
    "name": "Bala Ged Recovery // Bala Ged Sanctuary",
    "layout": "modal_dfc",
    "prices": {"usd": "2.50", "usd_foil": None},
    "legalities": {"commander": "legal", "vintage": "legal", "legacy": "legal",
                   "modern": "legal", "pioneer": "not_legal", "standard": "not_legal"},
}

# Token card stored under the front-face name only (as in oracle_cards).
# ManaPool may append the token face: "Stoneforged Blade // Germ".
_TOKEN_STONEFORGED = {
    "id": "jkl",
    "name": "Stoneforged Blade",
    "layout": "token",
    "prices": {"usd": "0.25", "usd_foil": None},
    "legalities": {},
}


def test_is_sanctioned_legal_card():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_BOLT], d))
        assert bulk.is_sanctioned("Lightning Bolt") is True


def test_is_sanctioned_case_insensitive():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_BOLT], d))
        assert bulk.is_sanctioned("lightning bolt") is True


def test_is_not_sanctioned_playtest_card():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_PLANEQUAKE], d))
        assert bulk.is_sanctioned("Planequake") is False


def test_unknown_card_assumed_sanctioned():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([], d))
        assert bulk.is_sanctioned("Totally Unknown Card") is True


def test_get_market_price_nonfoil():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_BOLT], d))
        assert bulk.get_market_price("Lightning Bolt") == 1.25


def test_get_market_price_foil():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_BOLT], d))
        assert bulk.get_market_price("Lightning Bolt", foil=True) == 3.00


def test_get_market_price_unknown_returns_none():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([], d))
        assert bulk.get_market_price("Unknown Card") is None


def test_dfc_front_face_lookup():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_DFC], d))
        assert bulk.get_market_price("Bala Ged Recovery") == 2.50
        assert bulk.is_sanctioned("Bala Ged Recovery") is True


def test_missing_file_does_not_crash():
    bulk = ScryfallBulk(Path("/nonexistent/oracle.json"))
    assert bulk.is_sanctioned("Lightning Bolt") is True  # unknown → assumed sanctioned
    assert bulk.get_market_price("Lightning Bolt") is None
    assert bulk.available is False


# --- is_token ---

def test_is_token_simple():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_TOKEN_STONEFORGED], d))
        assert bulk.is_token("Stoneforged Blade") is True


def test_is_token_manapool_dfc_name():
    """ManaPool appends the token face; is_token must match on the front face alone."""
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_TOKEN_STONEFORGED], d))
        assert bulk.is_token("Stoneforged Blade // Germ") is True


def test_is_token_normal_dfc_not_flagged():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_DFC], d))
        assert bulk.is_token("Bala Ged Recovery // Bala Ged Sanctuary") is False
        assert bulk.is_token("Bala Ged Recovery") is False


def test_is_token_unknown_card_returns_false():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([], d))
        assert bulk.is_token("Unknown Card") is False


# --- is_recently_released ---

def _card_with_set(set_code: str, released_at: str) -> dict:
    return {
        "id": set_code,
        "name": f"Test Card {set_code}",
        "set": set_code,
        "released_at": released_at,
        "prices": {"usd": "1.00", "usd_foil": None},
        "legalities": {"vintage": "legal"},
    }


def test_recently_released_new_set():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set("new1", yesterday)], d))
        assert bulk.is_recently_released("new1", days=30) is True


def test_recently_released_old_set():
    old = (date.today() - timedelta(days=60)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set("old1", old)], d))
        assert bulk.is_recently_released("old1", days=30) is False


def test_recently_released_presale_set():
    """Future release dates (presale) are treated as recently released."""
    future = (date.today() + timedelta(days=5)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set("pre1", future)], d))
        assert bulk.is_recently_released("pre1", days=30) is True


def test_recently_released_unknown_set_not_excluded():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([], d))
        assert bulk.is_recently_released("zzz", days=30) is False


def test_recently_released_case_insensitive():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set("MKM", yesterday)], d))
        assert bulk.is_recently_released("mkm", days=30) is True
        assert bulk.is_recently_released("MKM", days=30) is True


# --- is_playable_set ---

def _card_with_set_type(set_code: str, set_type: str) -> dict:
    return {
        "id": set_code,
        "name": f"Test Card {set_code}",
        "set": set_code,
        "set_type": set_type,
        "released_at": "2020-01-01",
        "prices": {"usd": "1.00", "usd_foil": None},
        "legalities": {"vintage": "legal"},
    }


def test_is_playable_set_normal_expansion():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set_type("MIR", "expansion")], d))
        assert bulk.is_playable_set("MIR") is True


def test_is_playable_set_memorabilia_excluded():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set_type("WC04", "memorabilia")], d))
        assert bulk.is_playable_set("WC04") is False


def test_is_playable_set_funny_excluded():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set_type("UNH", "funny")], d))
        assert bulk.is_playable_set("UNH") is False


def test_is_playable_set_token_excluded():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set_type("TAKH", "token")], d))
        assert bulk.is_playable_set("TAKH") is False


def test_is_playable_set_unknown_set_not_excluded():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([], d))
        assert bulk.is_playable_set("ZZZ") is True


def test_is_playable_set_case_insensitive():
    with tempfile.TemporaryDirectory() as d:
        bulk = ScryfallBulk(_write_bulk([_card_with_set_type("WC04", "memorabilia")], d))
        assert bulk.is_playable_set("wc04") is False
        assert bulk.is_playable_set("WC04") is False
