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
    assert listings[0].condition == Condition.NM
    assert listings[1].condition == Condition.LP
    assert listings[2].condition == Condition.HP


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
