import pytest
import responses as resp_mock

from manabot.api.scryfall import ScryfallAPIError, ScryfallClient

BASE = "https://api.scryfall.com"
BOLT_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"


def _card(scryfall_id=BOLT_ID, name="Lightning Bolt", flavor_name=None, promo_types=None) -> dict:
    return {
        "id": scryfall_id,
        "name": name,
        "flavor_name": flavor_name,
        "promo_types": promo_types or [],
        "set_type": "expansion",
    }


@pytest.fixture
def client() -> ScryfallClient:
    return ScryfallClient()


# --- lookup_by_name ---

@resp_mock.activate
def test_lookup_by_name_exact(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", json=_card())
    result = client.lookup_by_name("Lightning Bolt")
    assert result == BOLT_ID


@resp_mock.activate
def test_lookup_by_name_falls_back_to_fuzzy(client):
    # Exact fails with 404, fuzzy succeeds
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", status=404, json={"code": "not_found", "status": 404})
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", json=_card())
    result = client.lookup_by_name("Lightnin Bolt")
    assert result == BOLT_ID


@resp_mock.activate
def test_lookup_by_name_returns_none_when_not_found(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", status=404, json={"code": "not_found", "status": 404})
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", status=404, json={"code": "not_found", "status": 404})
    result = client.lookup_by_name("zzz not a card zzz")
    assert result is None


# --- get_card_metadata ---

@resp_mock.activate
def test_get_card_metadata_returns_data(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card())
    meta = client.get_card_metadata(BOLT_ID)
    assert meta["id"] == BOLT_ID
    assert meta["name"] == "Lightning Bolt"


@resp_mock.activate
def test_get_card_metadata_cached(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card())
    client.get_card_metadata(BOLT_ID)
    client.get_card_metadata(BOLT_ID)  # second call should use cache
    assert len(resp_mock.calls) == 1  # only one HTTP call made


@resp_mock.activate
def test_get_card_metadata_http_error(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", status=404)
    with pytest.raises(ScryfallAPIError, match="404"):
        client.get_card_metadata(BOLT_ID)


# --- is_in_universe ---

@resp_mock.activate
def test_is_in_universe_standard_card(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card())
    assert client.is_in_universe(BOLT_ID) is True


@resp_mock.activate
def test_is_in_universe_false_when_flavor_name_set(client):
    # Card with alternate universe name (e.g. "Wild Rose Rebellion" for Counterspell)
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card(flavor_name="Wild Rose Rebellion"))
    assert client.is_in_universe(BOLT_ID) is False


@resp_mock.activate
def test_is_in_universe_false_when_universesbeyond(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card(promo_types=["universesbeyond"]))
    assert client.is_in_universe(BOLT_ID) is False


@resp_mock.activate
def test_is_in_universe_false_when_sourcematerial(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card(promo_types=["sourcematerial", "boosterfun"]))
    assert client.is_in_universe(BOLT_ID) is False


@resp_mock.activate
def test_is_in_universe_true_with_irrelevant_promo_types(client):
    # "boosterfun" is not an exclusion criterion
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", json=_card(promo_types=["boosterfun"]))
    assert client.is_in_universe(BOLT_ID) is True


@resp_mock.activate
def test_is_in_universe_returns_none_on_fetch_error(client):
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/{BOLT_ID}", status=500)
    result = client.is_in_universe(BOLT_ID)
    assert result is None


# --- enrich_buylist ---

@resp_mock.activate
def test_enrich_buylist_resolves_missing_ids():
    from manabot.buylist import enrich_buylist
    from manabot.models import BuyListItem, Condition

    item = BuyListItem(
        card_name="Lightning Bolt",
        scryfall_id=None,
        target_quantity=4,
        max_price_usd=2.00,
        min_condition=Condition.LP,
    )
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", json=_card())
    client = ScryfallClient()
    enrich_buylist([item], client)
    assert item.scryfall_id == BOLT_ID


@resp_mock.activate
def test_enrich_buylist_skips_items_with_existing_id():
    from manabot.buylist import enrich_buylist
    from manabot.models import BuyListItem, Condition

    item = BuyListItem(
        card_name="Lightning Bolt",
        scryfall_id=BOLT_ID,
        target_quantity=4,
        max_price_usd=2.00,
        min_condition=Condition.LP,
    )
    client = ScryfallClient()
    enrich_buylist([item], client)
    assert len(resp_mock.calls) == 0  # no Scryfall call made


@resp_mock.activate
def test_enrich_buylist_keeps_none_when_not_found():
    from manabot.buylist import enrich_buylist
    from manabot.models import BuyListItem, Condition

    item = BuyListItem(
        card_name="zzz not a card zzz",
        scryfall_id=None,
        target_quantity=1,
        max_price_usd=1.00,
        min_condition=Condition.NM,
    )
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", status=404, json={"code": "not_found", "status": 404})
    resp_mock.add(resp_mock.GET, f"{BASE}/cards/named", status=404, json={"code": "not_found", "status": 404})
    client = ScryfallClient()
    enrich_buylist([item], client)
    assert item.scryfall_id is None
