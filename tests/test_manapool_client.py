import json
from pathlib import Path

import pytest
import responses as resp_mock

from manabot.api.manapool import ManaPoolAPIError, ManaPoolClient
from manabot.models import Condition, Finish

FIXTURE = Path(__file__).parent / "fixtures" / "sample_prices.json"
BASE = "https://manapool.com/api/v1"


def sample_data() -> list[dict]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def client() -> ManaPoolClient:
    return ManaPoolClient(email="test@example.com", token="test-token")


@resp_mock.activate
def test_get_singles_returns_correct_count(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    assert len(listings) == 5


@resp_mock.activate
def test_scryfall_id_parsed(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    assert listings[0].scryfall_id == "e3285e6b-3e79-4d7c-bf96-d920f973b122"


@resp_mock.activate
def test_condition_parsed(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    # Row 0: only price_cents_nm set → NM listing
    assert listings[0].condition == Condition.NM
    # Row 1: only price_cents_lp_plus set → LP listing
    assert listings[1].condition == Condition.LP
    # Row 2: only price_cents set → MP listing (cheapest any-condition tier)
    assert listings[2].condition == Condition.MP


@resp_mock.activate
def test_finish_parsed(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    assert listings[0].finish == Finish.NONFOIL
    assert listings[4].finish == Finish.FOIL


@resp_mock.activate
def test_price_parsed(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    assert listings[0].price_usd == pytest.approx(1.25)
    assert listings[3].price_usd == pytest.approx(450.00)


@resp_mock.activate
def test_set_code_uppercased(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=sample_data())
    listings = client.get_singles_prices()
    assert listings[0].set_code == "LEB"


@resp_mock.activate
def test_http_error_raises_domain_exception(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", status=401, body="Unauthorized")
    with pytest.raises(ManaPoolAPIError, match="401"):
        client.get_singles_prices()


@resp_mock.activate
def test_connection_error_raises_domain_exception(client):
    import requests as reqs
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", body=reqs.ConnectionError("failed"))
    with pytest.raises(ManaPoolAPIError):
        client.get_singles_prices()


@resp_mock.activate
def test_empty_response(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=[])
    listings = client.get_singles_prices()
    assert listings == []


@resp_mock.activate
def test_envelope_response_unwrapped(client):
    """API returns {"meta": {...}, "data": [...]} envelope."""
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles",
                  json={"meta": {"as_of": "2026-01-01"}, "data": sample_data()})
    listings = client.get_singles_prices()
    assert len(listings) == 5


@resp_mock.activate
def test_expand_listings_produces_per_condition_rows(client):
    """One API row with multiple price tiers expands into multiple listings."""
    row = {
        "name": "Test Card",
        "set_code": "TST",
        "scryfall_id": "abc",
        "available_quantity": 10,
        "price_cents_nm": 200,
        "price_cents_lp_plus": 150,
        "price_cents": 100,
        "price_cents_nm_foil": 300,
        "price_cents_lp_plus_foil": 0,
        "price_cents_foil": 0,
    }
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json={"data": [row]})
    listings = client.get_singles_prices()
    from manabot.models import Condition, Finish
    conditions = {(l.condition, l.finish): l.price_usd for l in listings}
    assert conditions[(Condition.NM, Finish.NONFOIL)] == pytest.approx(2.00)
    assert conditions[(Condition.LP, Finish.NONFOIL)] == pytest.approx(1.50)
    assert conditions[(Condition.MP, Finish.NONFOIL)] == pytest.approx(1.00)
    assert conditions[(Condition.NM, Finish.FOIL)] == pytest.approx(3.00)
    assert len(listings) == 4  # lp_plus_foil and foil were 0 → excluded


def test_parse_listing_unknown_condition(client):
    from datetime import datetime, timezone
    raw = {
        "scryfall_id": "abc",
        "name": "Test Card",
        "set": "TST",
        "condition": "Unknown",
        "finish": "nonfoil",
        "price": 1.0,
        "quantity": 1,
        "seller_id": "s1",
    }
    listing = client._parse_listing(raw, datetime.now(timezone.utc))
    # Unknown condition should fall back to LP (lenient default)
    assert listing.condition == Condition.LP


# ---------------------------------------------------------------------------
# get_card_ids_by_scryfall_id
# ---------------------------------------------------------------------------

def test_get_card_ids_empty_input_returns_empty_no_call(client):
    assert client.get_card_ids_by_scryfall_id([]) == {}


@resp_mock.activate
def test_get_card_ids_single_batch(client):
    resp_mock.add(
        resp_mock.GET, f"{BASE}/products/singles",
        json={
            "meta": {"as_of": "2026-01-01T00:00:00Z"},
            "data": [
                {"scryfall_id": "sf-1", "card_id": "card-1", "name": "Lightning Bolt"},
                {"scryfall_id": "sf-2", "card_id": "card-2", "name": "Counterspell"},
            ],
        },
    )
    result = client.get_card_ids_by_scryfall_id(["sf-1", "sf-2"])
    assert result == {"sf-1": "card-1", "sf-2": "card-2"}


@resp_mock.activate
def test_get_card_ids_missing_id_absent_from_result(client):
    resp_mock.add(
        resp_mock.GET, f"{BASE}/products/singles",
        json={"meta": {"as_of": "2026-01-01T00:00:00Z"}, "data": []},
    )
    result = client.get_card_ids_by_scryfall_id(["sf-unknown"])
    assert result == {}


@resp_mock.activate
def test_get_card_ids_dedupes_input(client):
    """Duplicate scryfall_ids in the input are sent only once."""
    calls = []

    def _callback(request):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(request.url).query)
        calls.append(qs.get("scryfall_ids", []))
        return (200, {}, json.dumps({
            "meta": {"as_of": "2026-01-01T00:00:00Z"},
            "data": [{"scryfall_id": "sf-1", "card_id": "card-1"}],
        }))

    resp_mock.add_callback(resp_mock.GET, f"{BASE}/products/singles", callback=_callback)
    client.get_card_ids_by_scryfall_id(["sf-1", "sf-1", "sf-1"])
    assert len(calls) == 1
    assert calls[0] == ["sf-1"]


@resp_mock.activate
def test_get_card_ids_batches_at_100(client):
    """More than 100 IDs are split across multiple calls."""
    call_sizes = []

    def _callback(request):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(request.url).query)
        batch = qs.get("scryfall_ids", [])
        call_sizes.append(len(batch))
        return (200, {}, json.dumps({
            "meta": {"as_of": "2026-01-01T00:00:00Z"},
            "data": [{"scryfall_id": sid, "card_id": f"card-{sid}"} for sid in batch],
        }))

    resp_mock.add_callback(resp_mock.GET, f"{BASE}/products/singles", callback=_callback)
    ids = [f"sf-{i}" for i in range(150)]
    result = client.get_card_ids_by_scryfall_id(ids)
    assert call_sizes == [100, 50]
    assert len(result) == 150


# ---------------------------------------------------------------------------
# get_seller_inventory / _parse_seller_listing
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_get_seller_inventory_parses_number_and_mtgjson_id(client):
    """Regression: number/mtgjson_id were present in the raw API response all along
    (confirmed live) but silently discarded -- they're what's needed to disambiguate
    reused token-sheet names like double-sided token "Lander" backs (see
    pricer.apply_double_sided_upgrades)."""
    resp_mock.add(
        resp_mock.GET, f"{BASE}/seller/inventory",
        json={
            "inventory": [{
                "id": "inv-1",
                "product": {
                    "id": "prod-1",
                    "single": {
                        "scryfall_id": "human-soldier-2",
                        "mtgjson_id": "mtgjson-2-7",
                        "name": "Human Soldier // Lander",
                        "set": "teoe",
                        "number": "2-7",
                        "language_id": "EN",
                        "condition_id": "NM",
                        "finish_id": "NF",
                    },
                },
                "price_cents": 15,
                "quantity": 2,
            }],
            "pagination": {"next_cursor": None},
        },
    )
    listings = client.get_seller_inventory()
    assert len(listings) == 1
    assert listings[0].number == "2-7"
    assert listings[0].mtgjson_id == "mtgjson-2-7"
    assert listings[0].set_code == "TEOE"


@resp_mock.activate
def test_get_seller_inventory_defaults_number_when_absent(client):
    """Ordinary (non-token) listings won't have a number field -- must default to
    empty string, not crash or store None."""
    resp_mock.add(
        resp_mock.GET, f"{BASE}/seller/inventory",
        json={
            "inventory": [{
                "id": "inv-1",
                "product": {
                    "id": "prod-1",
                    "single": {
                        "scryfall_id": "bolt-id",
                        "name": "Lightning Bolt",
                        "set": "lea",
                        "language_id": "EN",
                        "condition_id": "NM",
                        "finish_id": "NF",
                    },
                },
                "price_cents": 150,
                "quantity": 4,
            }],
            "pagination": {"next_cursor": None},
        },
    )
    listings = client.get_seller_inventory()
    assert listings[0].number == ""
    assert listings[0].mtgjson_id == ""
