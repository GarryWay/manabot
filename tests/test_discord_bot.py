from pathlib import Path

import pytest
import responses as resp_mock

from manabot.buylist import load_buylist
from manabot.discord_bot import (
    _add_cards_sync,
    _attach_pinned_printings,
    _mass_entry_kwargs,
    _parse_scryfall_id,
    _resolve_printing_id,
)
from manabot.models import BuyListItem, CartRequestItem, Condition, Finish

VALID_ID = "bd8fa327-dd41-4737-8f19-2cf5eb1f7cdd"
SCRYFALL_BASE = "https://api.scryfall.com"


class _StubScryfallClient:
    """Minimal stand-in for ScryfallClient.lookup_by_set_number, no network involved."""
    def __init__(self, result: str | None):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def lookup_by_set_number(self, set_code: str, collector_number: str) -> str | None:
        self.calls.append((set_code, collector_number))
        return self._result


def test_parse_scryfall_id_normalizes_case_and_whitespace():
    assert _parse_scryfall_id(f"  {VALID_ID.upper()}  ") == VALID_ID


def test_parse_scryfall_id_rejects_malformed_input():
    with pytest.raises(ValueError):
        _parse_scryfall_id("not-a-uuid")


def test_add_cards_sync_accepts_scryfall_id(tmp_path: Path):
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, f"Sol Ring;1;5.00;NM;;any;{VALID_ID}", "tester", 1)

    assert errors == []
    assert len(added) == 1
    assert VALID_ID in added[0]

    items = load_buylist(path)
    assert items[0].scryfall_id == VALID_ID
    assert items[0].allowed_sets == []


def test_add_cards_sync_scryfall_id_overrides_set_code(tmp_path: Path):
    """A set code alongside a scryfall_id is dropped, not kept — see the redundancy
    note in cmd_add_card: a mismatched allowed_sets would zero out every candidate
    once the id has already pinned one exact printing.
    """
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, f"Black Lotus;1;100000;NM;LEA;any;{VALID_ID}", "tester", 1)

    assert errors == []
    items = load_buylist(path)
    assert items[0].scryfall_id == VALID_ID
    assert items[0].allowed_sets == []


def test_add_cards_sync_rejects_malformed_scryfall_id(tmp_path: Path):
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, "Bad Card;1;1;NM;;any;not-a-uuid", "tester", 1)

    assert added == []
    assert len(errors) == 1
    assert "Bad Card" in errors[0]
    assert not path.exists()


def test_add_cards_sync_scryfall_id_field_is_optional(tmp_path: Path):
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, "Lightning Bolt;4;1.50;LP", "tester", 1)

    assert errors == []
    items = load_buylist(path)
    assert items[0].scryfall_id is None


# --- _resolve_printing_id ---

def test_resolve_printing_id_returns_id_on_success():
    client = _StubScryfallClient(VALID_ID)
    result = _resolve_printing_id(client, "LEA", "233")
    assert result == VALID_ID
    assert client.calls == [("LEA", "233")]


def test_resolve_printing_id_raises_when_not_found():
    client = _StubScryfallClient(None)
    with pytest.raises(ValueError, match="No Scryfall card found"):
        _resolve_printing_id(client, "XYZ", "999")


def test_resolve_printing_id_raises_when_collector_number_missing():
    client = _StubScryfallClient(VALID_ID)
    with pytest.raises(ValueError, match="collector_number needs set_code too"):
        _resolve_printing_id(client, "LEA", "")
    assert client.calls == []  # never even called the client


def test_resolve_printing_id_raises_when_set_code_missing():
    client = _StubScryfallClient(VALID_ID)
    with pytest.raises(ValueError, match="collector_number needs set_code too"):
        _resolve_printing_id(client, "", "233")
    assert client.calls == []


# --- _add_cards_sync: set_code + collector_number ---

@resp_mock.activate
def test_add_cards_sync_resolves_collector_number(tmp_path: Path):
    resp_mock.add(resp_mock.GET, f"{SCRYFALL_BASE}/cards/lea/233", json={"id": VALID_ID})
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, "Black Lotus;1;50000;NM;LEA;any;;233", "tester", 1)

    assert errors == []
    assert len(added) == 1
    assert VALID_ID in added[0]

    items = load_buylist(path)
    assert items[0].scryfall_id == VALID_ID
    assert items[0].allowed_sets == []  # the plain set restriction is dropped in favor of the pin


@resp_mock.activate
def test_add_cards_sync_explicit_scryfall_id_wins_over_collector_number(tmp_path: Path):
    other_id = "11111111-2222-3333-4444-555555555555"
    path = tmp_path / "buylist.csv"
    # No Scryfall call should even be made — the explicit id takes precedence.
    added, errors = _add_cards_sync(
        path, f"Black Lotus;1;50000;NM;LEA;any;{other_id};233", "tester", 1
    )

    assert errors == []
    items = load_buylist(path)
    assert items[0].scryfall_id == other_id
    assert len(resp_mock.calls) == 0


@resp_mock.activate
def test_add_cards_sync_collector_number_requires_set_code(tmp_path: Path):
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, "Black Lotus;1;50000;NM;;any;;233", "tester", 1)

    assert added == []
    assert len(errors) == 1
    assert "collector_number needs set_code too" in errors[0]
    assert len(resp_mock.calls) == 0


@resp_mock.activate
def test_add_cards_sync_collector_number_not_found(tmp_path: Path):
    resp_mock.add(
        resp_mock.GET, f"{SCRYFALL_BASE}/cards/lea/9999",
        status=404, json={"code": "not_found", "status": 404},
    )
    path = tmp_path / "buylist.csv"
    added, errors = _add_cards_sync(path, "Black Lotus;1;50000;NM;LEA;any;;9999", "tester", 1)

    assert added == []
    assert len(errors) == 1
    assert "No Scryfall card found" in errors[0]


def _cart_item(scryfall_id: str | None, set_code: str = "LEA") -> CartRequestItem:
    return CartRequestItem(
        buy_list_item=BuyListItem(
            card_name="Black Lotus", target_quantity=1, max_price_usd=50000.0,
            min_condition=Condition.NM, scryfall_id=scryfall_id,
        ),
        set_code=set_code, estimated_price=40000.0, estimated_margin=10000.0,
        condition_ids=["NM"], finish_ids=["NF"],
    )


class _StubMetadataClient:
    """Stand-in for ScryfallClient.get_card_metadata, no network involved."""
    def __init__(self, by_id: dict[str, dict]):
        self._by_id = by_id

    def get_card_metadata(self, scryfall_id: str) -> dict:
        if scryfall_id not in self._by_id:
            raise KeyError(scryfall_id)
        return self._by_id[scryfall_id]


def test_mass_entry_kwargs_plain_item_has_no_printing_pin():
    kw = _mass_entry_kwargs([{"quantity": 4, "card_name": "Lightning Bolt", "set_code": "2X2"}])
    assert kw["content"] == "**Paste into ManaPool's Mass Entry:**\n```\n4 Lightning Bolt\n```"


def test_mass_entry_kwargs_pinned_item_appends_set_and_number():
    kw = _mass_entry_kwargs([
        {"quantity": 1, "card_name": "Black Lotus", "set_code": "LEA", "collector_number": "232"},
    ])
    assert kw["content"] == "**Paste into ManaPool's Mass Entry:**\n```\n1 Black Lotus [LEA] 232\n```"


def test_mass_entry_kwargs_mixed_pinned_and_unpinned():
    kw = _mass_entry_kwargs([
        {"quantity": 4, "card_name": "Lightning Bolt", "set_code": "2X2"},
        {"quantity": 1, "card_name": "Black Lotus", "set_code": "LEA", "collector_number": "232"},
    ])
    assert kw["content"] == (
        "**Paste into ManaPool's Mass Entry:**\n```\n"
        "4 Lightning Bolt\n1 Black Lotus [LEA] 232\n```"
    )


def test_attach_pinned_printings_resolves_collector_number_for_pinned_item():
    items_data = [{"card_name": "Black Lotus", "quantity": 1, "set_code": "LEA"}]
    cart_items = [_cart_item(scryfall_id=VALID_ID)]
    client = _StubMetadataClient({VALID_ID: {"collector_number": "232"}})

    _attach_pinned_printings(items_data, cart_items, client)

    assert items_data[0]["collector_number"] == "232"


def test_attach_pinned_printings_skips_unpinned_item():
    items_data = [{"card_name": "Black Lotus", "quantity": 1, "set_code": "LEA"}]
    cart_items = [_cart_item(scryfall_id=None)]
    client = _StubMetadataClient({})

    _attach_pinned_printings(items_data, cart_items, client)

    assert "collector_number" not in items_data[0]


def test_attach_pinned_printings_lookup_failure_leaves_item_unpinned():
    items_data = [{"card_name": "Black Lotus", "quantity": 1, "set_code": "LEA"}]
    cart_items = [_cart_item(scryfall_id=VALID_ID)]
    client = _StubMetadataClient({})  # no entry for VALID_ID -> KeyError inside get_card_metadata

    _attach_pinned_printings(items_data, cart_items, client)

    assert "collector_number" not in items_data[0]
