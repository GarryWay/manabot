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
