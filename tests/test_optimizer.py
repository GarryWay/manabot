"""Tests for the cart optimizer module and ManaPoolClient.run_optimizer."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import responses as resp_mock

from manabot.api.manapool import ManaPool409Error, ManaPoolAPIError, ManaPoolClient
from manabot.models import BuyListItem, CartRequestItem, CartResult, Condition, Finish, MatchResult, MatchStatus, PriceListing
from manabot.optimizer import (
    _bisect_batch_removal,
    _build_optimizer_payload,
    _acceptable_conditions,
    _chunk,
    _group_by_seller,
    _is_better,
    _resolve_card_ids,
    _select_within_budget,
    _try_batch,
    build_request_items,
    find_best_cart,
    try_add_items,
)

MANAPOOL_BASE = "https://manapool.com/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(
    name: str = "Lightning Bolt",
    max_price: float = 2.00,
    min_cond: Condition = Condition.LP,
    foil: Finish = Finish.NONFOIL,
    qty: int = 4,
    allowed_sets: list[str] | None = None,
) -> BuyListItem:
    return BuyListItem(
        card_name=name,
        target_quantity=qty,
        max_price_usd=max_price,
        min_condition=min_cond,
        foil=foil,
        allowed_sets=allowed_sets or [],
    )


def _listing(
    scryfall_id: str = "abc-123",
    name: str = "Lightning Bolt",
    set_code: str = "M10",
    condition: Condition = Condition.NM,
    finish: Finish = Finish.NONFOIL,
    price: float = 1.50,
    qty: int = 4,
) -> PriceListing:
    return PriceListing(
        scryfall_id=scryfall_id,
        card_name=name,
        set_code=set_code,
        condition=condition,
        finish=finish,
        price_usd=price,
        quantity_available=qty,
        seller_id="seller1",
        fetched_at=datetime.now(timezone.utc),
    )


def _matched(buy_item: BuyListItem, listing: PriceListing) -> MatchResult:
    return MatchResult(
        buy_list_item=buy_item,
        listings=[listing],
        best_price=listing.price_usd,
        is_good_buy=True,
        status=MatchStatus.MATCHED,
    )


def _optimizer_ndjson(
    subtotal_cents: int = 600,
    shipping_cents: int = 200,
    fee_cents: int = 50,
    inventory_id: str = "inv-1",
) -> str:
    """NDJSON body: one stats line, one cart line (simulates streamed response)."""
    stats = json.dumps({"stats": {"response_time": 42}})
    total = subtotal_cents + shipping_cents + fee_cents
    cart = json.dumps({
        "cart": [{"inventory_id": inventory_id, "quantity_selected": 4}],
        "totals": {
            "subtotal_cents": subtotal_cents,
            "shipping_cents": shipping_cents,
            "buyer_fee_cents": fee_cents,
            "total_cents": total,
            "seller_count": 1,
        },
    })
    return f"{stats}\n{cart}\n"


def _mock_card_ids() -> None:
    """Register a GET /products/singles mock that resolves any scryfall_ids to a
    deterministic card_id, for tests that exercise find_best_cart() end-to-end
    (it always resolves card_id via this endpoint before calling the optimizer)."""
    def _callback(request):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(request.url).query)
        scryfall_ids = qs.get("scryfall_ids", [])
        data = [{"scryfall_id": sid, "card_id": f"card-{sid}"} for sid in scryfall_ids]
        body = json.dumps({"meta": {"as_of": "2026-01-01T00:00:00Z"}, "data": data})
        return (200, {"Content-Type": "application/json"}, body)

    resp_mock.add_callback(
        resp_mock.GET, f"{MANAPOOL_BASE}/products/singles", callback=_callback,
    )


def _mock_optimizer_by_names(scorer) -> None:
    """Register a POST /buyer/optimizer callback mock that computes its response from
    which card names are present in the request payload, via
    scorer(names: list[str]) -> (subtotal_cents, shipping_cents, fee_cents).

    Deterministic regardless of call order or concurrency, unlike a FIFO-queued
    resp_mock.add() sequence — needed for find_best_cart() tests now that Phase 2 fires
    some trials concurrently (_bisect_batch_removal's parallel probe) rather than
    strictly one at a time.
    """
    def _callback(request):
        payload = json.loads(request.body)
        names = [entry["name"] for entry in payload["cart"]]
        subtotal_cents, shipping_cents, fee_cents = scorer(names)
        body = _optimizer_ndjson(subtotal_cents=subtotal_cents, shipping_cents=shipping_cents, fee_cents=fee_cents)
        return (200, {"Content-Type": "application/x-ndjson"}, body)

    resp_mock.add_callback(resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer", callback=_callback)


@pytest.fixture
def mp_client() -> ManaPoolClient:
    return ManaPoolClient(email="test@example.com", token="test-token")


# ---------------------------------------------------------------------------
# _acceptable_conditions
# ---------------------------------------------------------------------------

def test_acceptable_conditions_nm_only():
    assert _acceptable_conditions(Condition.NM) == ["NM"]


def test_acceptable_conditions_lp_includes_nm():
    assert _acceptable_conditions(Condition.LP) == ["NM", "LP"]


def test_acceptable_conditions_mp():
    assert _acceptable_conditions(Condition.MP) == ["NM", "LP", "MP"]


def test_acceptable_conditions_dmg_includes_all():
    result = _acceptable_conditions(Condition.DMG)
    assert result == ["NM", "LP", "MP", "HP", "DMG"]


# ---------------------------------------------------------------------------
# build_request_items
# ---------------------------------------------------------------------------

def test_build_request_items_matched():
    item = _item()
    listing = _listing()
    result = _matched(item, listing)

    cart_items = build_request_items([result])
    assert len(cart_items) == 1
    ci = cart_items[0]
    assert ci.buy_list_item is item
    assert ci.set_code == "M10"
    assert ci.estimated_price == pytest.approx(1.50)
    assert ci.estimated_margin == pytest.approx(0.50)


def test_build_request_items_skips_unresolved():
    result = MatchResult(buy_list_item=_item(), status=MatchStatus.UNRESOLVED)
    assert build_request_items([result]) == []


def test_build_request_items_skips_no_listings():
    result = MatchResult(
        buy_list_item=_item(),
        listings=[],
        best_price=None,
        status=MatchStatus.MATCHED,
    )
    assert build_request_items([result]) == []


def test_build_request_items_excludes_over_threshold_default():
    item = _item(max_price=1.00)
    listing = _listing(price=1.20)
    result = _matched(item, listing)
    result.best_price = 1.20
    # Default threshold 0% — $1.20 > $1.00
    assert build_request_items([result], over_budget_pct=0.0) == []


def test_build_request_items_includes_within_threshold():
    item = _item(max_price=1.00)
    listing = _listing(price=1.15)
    result = _matched(item, listing)
    result.best_price = 1.15
    # 20% threshold — $1.15 ≤ $1.20
    items = build_request_items([result], over_budget_pct=20.0)
    assert len(items) == 1
    assert items[0].estimated_margin == pytest.approx(-0.15)


def test_build_request_items_condition_ids_from_min_condition():
    item = _item(min_cond=Condition.MP)
    result = _matched(item, _listing())
    ci = build_request_items([result])[0]
    assert ci.condition_ids == ["NM", "LP", "MP"]


def test_build_request_items_finish_nonfoil():
    result = _matched(_item(foil=Finish.NONFOIL), _listing())
    assert build_request_items([result])[0].finish_ids == ["NF"]


def test_build_request_items_finish_foil():
    result = _matched(_item(foil=Finish.FOIL), _listing(finish=Finish.FOIL))
    assert build_request_items([result])[0].finish_ids == ["FO"]


def test_build_request_items_finish_any():
    result = _matched(_item(foil=Finish.ANY), _listing())
    assert build_request_items([result])[0].finish_ids == ["NF", "FO"]


def test_build_request_items_picks_cheapest_set_code():
    item = _item()
    cheap = _listing(set_code="ICE", price=1.00)
    expensive = _listing(set_code="M10", price=2.00)
    result = MatchResult(
        buy_list_item=item,
        listings=[expensive, cheap],
        best_price=1.00,
        status=MatchStatus.MATCHED,
    )
    ci = build_request_items([result])[0]
    assert ci.set_code == "ICE"


def test_build_request_items_skips_non_playable_set():
    """When scryfall is provided, listings from non-playable sets are excluded."""
    from unittest.mock import MagicMock
    scryfall = MagicMock()
    # WC04 → not playable; M10 → playable
    scryfall.is_playable_set.side_effect = lambda code: code != "WC04"

    item = _item(max_price=5.00)
    wc04_listing = _listing(set_code="WC04", price=0.50)  # cheapest overall but non-playable
    m10_listing = _listing(set_code="M10", price=2.00)
    result = MatchResult(
        buy_list_item=item,
        listings=[wc04_listing, m10_listing],
        best_price=0.50,
        status=MatchStatus.MATCHED,
    )
    items = build_request_items([result], scryfall=scryfall)
    assert len(items) == 1
    assert items[0].set_code == "M10"
    assert items[0].estimated_price == pytest.approx(2.00)


def test_build_request_items_skips_entirely_when_all_non_playable():
    """When all listings are non-playable, the item is dropped."""
    from unittest.mock import MagicMock
    scryfall = MagicMock()
    scryfall.is_playable_set.return_value = False

    result = MatchResult(
        buy_list_item=_item(max_price=5.00),
        listings=[_listing(set_code="WC04", price=1.00)],
        best_price=1.00,
        status=MatchStatus.MATCHED,
    )
    assert build_request_items([result], scryfall=scryfall) == []


# ---------------------------------------------------------------------------
# _build_optimizer_payload
# ---------------------------------------------------------------------------

def test_build_optimizer_payload_structure():
    item = _item(name="Counterspell", max_price=2.00, qty=4, min_cond=Condition.LP)
    result = _matched(item, _listing(name="Counterspell", set_code="ICE"))
    cart_items = build_request_items([result])

    payload = _build_optimizer_payload(cart_items)
    assert len(payload) == 1
    p = payload[0]
    assert p["type"] == "mtg_single"
    assert p["name"] == "Counterspell"
    assert p["is_token"] is False
    assert p["include_non_sanctioned_legal"] is False
    assert p["language_ids"] == ["EN"]
    # No allowed_sets on item → set_code omitted so optimizer can search all sanctioned printings
    assert "set_code" not in p
    assert p["quantity_requested"] == 4
    assert p["condition_ids"] == ["NM", "LP"]
    assert p["finish_ids"] == ["NF"]


def test_build_optimizer_payload_includes_set_code_when_allowed_sets_specified():
    item = _item(name="Counterspell", max_price=2.00, qty=1, min_cond=Condition.NM,
                 allowed_sets=["ICE"])
    result = _matched(item, _listing(name="Counterspell", set_code="ICE"))
    cart_items = build_request_items([result])

    payload = _build_optimizer_payload(cart_items)
    assert payload[0]["set_code"] == "ICE"


def test_build_optimizer_payload_omits_card_id_when_unresolved():
    """build_request_items() doesn't resolve card_id itself (that's _resolve_card_ids,
    which needs a client) — so a freshly built item has no card_id yet."""
    item = _item(name="Counterspell")
    result = _matched(item, _listing(name="Counterspell"))
    cart_items = build_request_items([result])
    assert cart_items[0].card_id == ""
    payload = _build_optimizer_payload(cart_items)
    assert "card_id" not in payload[0]


def test_build_optimizer_payload_includes_card_id_when_resolved():
    item = _item(name="Counterspell")
    ci = CartRequestItem(
        buy_list_item=item, set_code="ICE", estimated_price=1.50, estimated_margin=0.50,
        condition_ids=["NM"], finish_ids=["NF"], scryfall_id="sf-1", card_id="card-1",
    )
    payload = _build_optimizer_payload([ci])
    assert payload[0]["card_id"] == "card-1"


def test_build_request_items_sets_scryfall_id_from_chosen_listing():
    item = _item(name="Lightning Bolt")
    result = _matched(item, _listing(scryfall_id="bolt-sf-id"))
    cart_items = build_request_items([result])
    assert cart_items[0].scryfall_id == "bolt-sf-id"
    assert cart_items[0].card_id == ""  # not resolved yet — needs _resolve_card_ids


# ---------------------------------------------------------------------------
# _resolve_card_ids
# ---------------------------------------------------------------------------

class _FakeCardIdClient:
    """Records requested scryfall_ids and returns a deterministic card_id for each,
    minus any explicitly listed as unresolvable."""
    def __init__(self, unresolvable: set[str] | None = None):
        self.requested: list[str] | None = None
        self._unresolvable = unresolvable or set()

    def get_card_ids_by_scryfall_id(self, scryfall_ids: list[str]) -> dict[str, str]:
        self.requested = list(scryfall_ids)
        return {
            sid: f"card-{sid}" for sid in scryfall_ids
            if sid not in self._unresolvable
        }


def test_resolve_card_ids_empty_list_returns_empty():
    assert _resolve_card_ids([], _FakeCardIdClient()) == []


def test_resolve_card_ids_sets_card_id_from_client():
    ci = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    ci.card_id = ""
    ci.scryfall_id = "bolt-sf"
    client = _FakeCardIdClient()
    resolved = _resolve_card_ids([ci], client)
    assert len(resolved) == 1
    assert resolved[0].card_id == "card-bolt-sf"


def test_resolve_card_ids_skips_lookup_for_already_resolved_items():
    """An item with a pre-supplied card_id shouldn't trigger a lookup at all."""
    ci = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    ci.card_id = "already-set"
    ci.scryfall_id = "bolt-sf"
    client = _FakeCardIdClient()
    resolved = _resolve_card_ids([ci], client)
    assert resolved[0].card_id == "already-set"
    assert client.requested is None  # never called


def test_resolve_card_ids_drops_unresolvable_items():
    ci = _cart_item("Mystery Card", est_price=1.00, margin=1.00)
    ci.card_id = ""
    ci.scryfall_id = "unknown-sf"
    client = _FakeCardIdClient(unresolvable={"unknown-sf"})
    assert _resolve_card_ids([ci], client) == []


def test_resolve_card_ids_drops_items_with_no_scryfall_id():
    ci = _cart_item("No Scryfall ID", est_price=1.00, margin=1.00)
    ci.card_id = ""
    ci.scryfall_id = ""
    client = _FakeCardIdClient()
    assert _resolve_card_ids([ci], client) == []


def test_resolve_card_ids_mixed_resolved_and_unresolved():
    resolved_ci = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    resolved_ci.card_id = ""
    resolved_ci.scryfall_id = "bolt-sf"
    unresolved_ci = _cart_item("Mystery Card", est_price=1.00, margin=1.00)
    unresolved_ci.card_id = ""
    unresolved_ci.scryfall_id = "unknown-sf"

    client = _FakeCardIdClient(unresolvable={"unknown-sf"})
    result = _resolve_card_ids([resolved_ci, unresolved_ci], client)
    assert len(result) == 1
    assert result[0].buy_list_item.card_name == "Lightning Bolt"


# ---------------------------------------------------------------------------
# ManaPoolClient.run_optimizer
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_run_optimizer_returns_last_cart(mp_client):
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=600, shipping_cents=200, fee_cents=50),
        content_type="application/x-ndjson",
    )
    result = mp_client.run_optimizer([{
        "type": "mtg_single", "name": "Lightning Bolt",
        "quantity_requested": 4, "condition_ids": ["NM"], "finish_ids": ["NF"],
    }])
    assert result["totals"]["total_cents"] == 850
    assert result["totals"]["subtotal_cents"] == 600
    assert len(result["cart"]) == 1
    assert result["cart"][0]["inventory_id"] == "inv-1"


@resp_mock.activate
def test_run_optimizer_skips_stats_lines(mp_client):
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(),
        content_type="application/x-ndjson",
    )
    result = mp_client.run_optimizer([])
    assert "stats" not in result
    assert "cart" in result


@resp_mock.activate
def test_run_optimizer_http_error_raises(mp_client):
    resp_mock.add(resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer", status=401)
    with pytest.raises(ManaPoolAPIError, match="401"):
        mp_client.run_optimizer([])


@resp_mock.activate
def test_run_optimizer_empty_stream_raises(mp_client):
    # Only stats line, no cart line
    body = json.dumps({"stats": {"response_time": 1}}) + "\n"
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=body,
        content_type="application/x-ndjson",
    )
    with pytest.raises(ManaPoolAPIError, match="no valid cart"):
        mp_client.run_optimizer([])


@resp_mock.activate
def test_run_optimizer_sends_correct_payload(mp_client):
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(),
        content_type="application/x-ndjson",
    )
    mp_client.run_optimizer(
        [{"type": "mtg_single", "name": "Bolt", "quantity_requested": 1}],
        model="balanced",
        destination_country="CA",
    )
    sent = json.loads(resp_mock.calls[0].request.body)
    assert sent["model"] == "balanced"
    assert sent["destination_country"] == "CA"
    assert sent["include_replacement_warehouses"] is False
    assert sent["ship_from_countries"] == ["US", "CA"]
    assert sent["cart"][0]["name"] == "Bolt"
    assert "filters" not in sent  # no UB/preorder filters requested


@resp_mock.activate
def test_run_optimizer_sends_filter_flags(mp_client):
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(),
        content_type="application/x-ndjson",
    )
    mp_client.run_optimizer(
        [],
        exclude_universes_beyond=True,
        exclude_preorder=True,
    )
    sent = json.loads(resp_mock.calls[0].request.body)
    sf = sent["filters"]["productFilters"]["singleFilters"]
    assert sf["excludeUniversesBeyond"] is True
    assert sf["excludePreRelease"] is True


# ---------------------------------------------------------------------------
# find_best_cart — exclude_ub derived from match_results
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_find_best_cart_exclude_ub_when_any_item_flagged(mp_client):
    _mock_card_ids()
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=200, shipping_cents=100, fee_cents=0),
        content_type="application/x-ndjson",
    )
    item_ub = _item(name="Sol Ring", max_price=2.00, qty=1, min_cond=Condition.NM)
    item_ub.exclude_ub = True
    item_ok = _item(name="Lightning Bolt", max_price=1.50, qty=1, min_cond=Condition.NM)

    find_best_cart([_matched(item_ub, _listing()), _matched(item_ok, _listing())], mp_client)

    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    sent = json.loads(post_calls[0].request.body)
    sf = sent["filters"]["productFilters"]["singleFilters"]
    assert sf["excludeUniversesBeyond"] is True


@resp_mock.activate
def test_find_best_cart_no_ub_filter_when_no_items_flagged(mp_client):
    _mock_card_ids()
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=200, shipping_cents=100, fee_cents=0),
        content_type="application/x-ndjson",
    )
    item = _item(name="Lightning Bolt", max_price=2.00, qty=1, min_cond=Condition.NM)

    find_best_cart([_matched(item, _listing())], mp_client)

    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    sent = json.loads(post_calls[0].request.body)
    assert "filters" not in sent


# ---------------------------------------------------------------------------
# find_best_cart
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_find_best_cart_happy_path(mp_client):
    # 4× Lightning Bolt at $1.50 each; max_price $2.00; total budget $8.00
    # Optimizer: subtotal $6, ship $2, fees $0.50 → total $8.50
    # net = $8.00 − $8.50 = −$0.50  (slightly over; still usable)
    _mock_card_ids()
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=600, shipping_cents=200, fee_cents=50),
        content_type="application/x-ndjson",
    )
    result = _matched(_item(max_price=3.00, qty=4), _listing(price=1.50))

    cart = find_best_cart([result], mp_client)
    assert cart is not None
    assert cart.total_usd == pytest.approx(8.50)
    assert cart.value_budget_usd == pytest.approx(12.00)  # 3.00 × 4
    assert cart.net_value_usd == pytest.approx(3.50)
    assert cart.is_profitable is True


def test_find_best_cart_no_eligible_items_returns_none(mp_client):
    item = _item(max_price=1.00)
    listing = _listing(price=2.00)
    result = _matched(item, listing)
    result.best_price = 2.00
    assert find_best_cart([result], mp_client) is None


def test_find_best_cart_no_matched_results_returns_none(mp_client):
    result = MatchResult(buy_list_item=_item(), status=MatchStatus.UNRESOLVED)
    assert find_best_cart([result], mp_client) is None


@resp_mock.activate
def test_find_best_cart_removes_item_when_net_improves(mp_client):
    """Over-budget item is removed when doing so improves net value."""
    _mock_card_ids()
    # Baseline (2 items): net worse because over-budget item adds cost with no shipping benefit
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=500, shipping_cents=300, fee_cents=50),
        content_type="application/x-ndjson",
    )
    # Trial (1 item, Bolt only): cheaper shipping → net improves
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=200, shipping_cents=100, fee_cents=20),
        content_type="application/x-ndjson",
    )

    # Lightning Bolt: $1.50, max $2.00, margin +$0.50 × 4 = +$2.00 budget contribution
    good = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=4), _listing(price=1.50))

    # Dark Ritual: $1.50, max $1.20, within 30% threshold but negative margin
    over_item = BuyListItem(
        card_name="Dark Ritual", target_quantity=2,
        max_price_usd=1.20, min_condition=Condition.NM,
    )
    over_listing = _listing(name="Dark Ritual", set_code="ICE", price=1.50, qty=2)
    over = MatchResult(
        buy_list_item=over_item, listings=[over_listing],
        best_price=1.50, status=MatchStatus.MATCHED,
    )

    cart = find_best_cart([good, over], mp_client, over_budget_pct=30.0, max_iterations=3)
    assert cart is not None
    assert len(cart.items) == 1
    assert cart.items[0].buy_list_item.card_name == "Lightning Bolt"


@resp_mock.activate
def test_find_best_cart_keeps_item_when_shipping_consolidation_wins(mp_client):
    """Over-budget item is kept when removing it (alone or together with the other
    candidate) worsens net value (shipping consolidation). Uses a name-keyed callback
    mock rather than a FIFO response queue, since Phase 2 may probe both removals
    concurrently instead of in a fixed order."""
    _mock_card_ids()

    def _scorer(names: list[str]) -> tuple[int, int, int]:
        has_bolt = "Lightning Bolt" in names
        has_ritual = "Dark Ritual" in names
        if has_bolt and has_ritual:
            # Baseline: value_budget = 2.00×4 + 1.20×2 = $10.40; total $6.50; net $3.90
            return 500, 100, 50
        if has_bolt:  # Dark Ritual removed — shipping skyrockets, net worsens
            return 400, 400, 40
        if has_ritual:  # Lightning Bolt removed — much worse, net worsens further
            return 150, 200, 10
        return 0, 0, 0  # both removed (shouldn't be reachable — nothing left to sell)

    _mock_optimizer_by_names(_scorer)

    good = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=4), _listing(price=1.50))

    over_item = BuyListItem(
        card_name="Dark Ritual", target_quantity=2,
        max_price_usd=1.20, min_condition=Condition.NM,
    )
    over_listing = _listing(name="Dark Ritual", set_code="ICE", price=1.50, qty=2)
    over = MatchResult(
        buy_list_item=over_item, listings=[over_listing],
        best_price=1.50, status=MatchStatus.MATCHED,
    )

    cart = find_best_cart([good, over], mp_client, over_budget_pct=30.0, max_iterations=3)
    assert cart is not None
    # Both items kept; baseline had better net value than removing either item
    assert len(cart.items) == 2
    names = {x.buy_list_item.card_name for x in cart.items}
    assert "Lightning Bolt" in names
    assert "Dark Ritual" in names


@resp_mock.activate
def test_find_best_cart_survives_phase2_trial_returning_no_valid_cart(mp_client):
    """A single failed removal trial (e.g. ManaPool's "no valid cart" response for that
    combination) must not crash the whole run -- it's treated like a rejected trial."""
    _mock_card_ids()
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=500, shipping_cents=100, fee_cents=50),
        content_type="application/x-ndjson",
    )
    # Trial (without Dark Ritual) fails outright — stats line only, no cart line.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=json.dumps({"stats": {"response_time": 1}}) + "\n",
        content_type="application/x-ndjson",
    )

    good = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=4), _listing(price=1.50))
    over_item = BuyListItem(
        card_name="Dark Ritual", target_quantity=2,
        max_price_usd=1.20, min_condition=Condition.NM,
    )
    over_listing = _listing(name="Dark Ritual", set_code="ICE", price=1.50, qty=2)
    over = MatchResult(
        buy_list_item=over_item, listings=[over_listing],
        best_price=1.50, status=MatchStatus.MATCHED,
    )

    cart = find_best_cart([good, over], mp_client, over_budget_pct=30.0, max_iterations=1)
    assert cart is not None
    # The failed trial is treated as "keep it" — baseline cart (both items) survives.
    assert len(cart.items) == 2


@resp_mock.activate
def test_find_best_cart_phase2_not_bounded_by_max_iterations(mp_client):
    """Phase 2 batches its removals (_bisect_batch_removal) instead of looping once per
    candidate, so it isn't limited by max_iterations the way the old one-at-a-time
    algorithm was. Five negative-margin candidates, max_iterations=1 -- the old algorithm
    could only ever have tried one of them; the batched version still removes all five,
    each validated standalone in parallel (not swept up in one blind batch call) plus
    one merge-validate call, well under 5 sequential one-at-a-time trials."""
    _mock_card_ids()

    def _scorer(names: list[str]) -> tuple[int, int, int]:
        # $1.00/ea flat, regardless of which subset remains — removing any negative-margin
        # candidate always helps (value_budget drops less than cost does per removed item,
        # since each costs more than its own $0.50 max_price).
        return len(names) * 100, 0, 0

    _mock_optimizer_by_names(_scorer)

    good = _matched(_item(name="Good", max_price=5.00, qty=1), _listing(name="Good", price=1.00))
    bad_results = []
    for i in range(5):
        name = f"Bad{i}"
        item = _item(name=name, max_price=0.50, qty=1)
        bad_results.append(_matched(item, _listing(name=name, price=1.00)))

    cart = find_best_cart([good, *bad_results], mp_client, over_budget_pct=100.0, max_iterations=1)

    assert cart is not None
    assert len(cart.items) == 1
    assert cart.items[0].buy_list_item.card_name == "Good"
    # baseline (1) + Phase 2a: 5 standalone probes (one per Bad item, in parallel) +
    # 1 merge-validate call = 7 total, despite max_iterations=1 -- the old per-item
    # loop would have needed 6+ sequential calls just for the 5 Bad items alone.
    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    assert len(post_calls) == 7


@resp_mock.activate
def test_find_best_cart_max_parallel_controls_group_size(mp_client):
    """max_parallel is a real, tunable knob on find_best_cart -- max_parallel=1 forces
    every Phase 2 candidate into a single group (no per-item parallelism at all),
    verifying it actually threads through to _bisect_batch_removal rather than being
    silently ignored."""
    _mock_card_ids()

    def _scorer(names: list[str]) -> tuple[int, int, int]:
        return len(names) * 100, 0, 0

    _mock_optimizer_by_names(_scorer)

    good = _matched(_item(name="Good", max_price=5.00, qty=1), _listing(name="Good", price=1.00))
    bad_results = []
    for i in range(5):
        name = f"Bad{i}"
        item = _item(name=name, max_price=0.50, qty=1)
        bad_results.append(_matched(item, _listing(name=name, price=1.00)))

    cart = find_best_cart(
        [good, *bad_results], mp_client, over_budget_pct=100.0, max_iterations=1, max_parallel=1,
    )

    assert cart is not None
    assert len(cart.items) == 1
    assert cart.items[0].buy_list_item.card_name == "Good"
    # With max_parallel=1, all 5 Bad candidates form one group: 1 probe call (removing
    # all 5 at once) + 1 merge-validate call = 2 Phase 2a calls, not 5 standalone probes.
    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    assert len(post_calls) == 1 + 2  # baseline + 2


# ---------------------------------------------------------------------------
# optimize CLI command (integration-level)
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_cli_optimize_dry_run(tmp_path):
    from click.testing import CliRunner
    from manabot.cli import cli
    import json as _json

    FIXTURE_PRICES = Path(__file__).parent / "fixtures" / "sample_prices.json"
    FIXTURE_BUYLIST = Path(__file__).parent / "fixtures" / "sample_buylist.csv"
    SCRYFALL_BASE = "https://api.scryfall.com"
    LOTUS_ID = "b0faa7f2-b547-42c4-a810-839da50dadfe"

    resp_mock.add(
        resp_mock.GET, "https://manapool.com/api/v1/prices/singles",
        json=_json.loads(FIXTURE_PRICES.read_text()),
    )
    resp_mock.add(
        resp_mock.GET, f"{SCRYFALL_BASE}/cards/{LOTUS_ID}",
        json={"id": LOTUS_ID, "name": "Black Lotus", "flavor_name": None, "promo_types": []},
    )

    runner = CliRunner(env={
        "MANAPOOL_EMAIL": "test@example.com",
        "MANAPOOL_TOKEN": "test-token",
        "DB_PATH": str(tmp_path / "test.db"),
        "REPORTS_DIR": str(tmp_path / "reports"),
        "DISCORD_WEBHOOK_URL": "",
    })
    result = runner.invoke(cli, [
        "optimize",
        "--buylist", str(FIXTURE_BUYLIST),
        "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()


# ---------------------------------------------------------------------------
# _select_within_budget
# ---------------------------------------------------------------------------

def test_select_within_budget_fits_all():
    items = [
        _cart_item("Bolt", est_price=1.50, margin=0.50, qty=4),   # cost $6.00
        _cart_item("Ritual", est_price=0.50, margin=0.25, qty=2), # cost $1.00
    ]
    selected = _select_within_budget(items, budget_usd=8.00)
    assert len(selected) == 2


def test_select_within_budget_skips_one_but_fits_smaller():
    # Budget $7.20 (equivalent to old $9.00 × 0.80 — caller now controls headroom).
    # Can't fit Bolt ($6) + Dual ($4): $10 > $7.20, skip Dual.
    # Can fit Bolt ($6) + Ritual ($1): $7 ≤ $7.20, include Ritual.
    items = [
        _cart_item("Bolt", est_price=1.50, margin=0.50, qty=4),    # cost $6.00, margin ×4 = $2.00
        _cart_item("Dual Land", est_price=4.00, margin=1.00, qty=1), # cost $4.00, margin ×1 = $1.00
        _cart_item("Ritual", est_price=0.50, margin=0.25, qty=2),   # cost $1.00, margin ×2 = $0.50
    ]
    # Sorted by total margin: Bolt ($2.00), Dual ($1.00), Ritual ($0.50)
    # Bolt ($6.00 ≤ $7.20): included. Dual ($4.00 → $10.00 > $7.20): skipped.
    # Ritual ($1.00 → $7.00 ≤ $7.20): included.
    selected = _select_within_budget(items, budget_usd=7.20)
    names = {x.buy_list_item.card_name for x in selected}
    assert "Bolt" in names
    assert "Ritual" in names
    assert "Dual Land" not in names


def test_select_within_budget_empty_when_nothing_fits():
    items = [_cart_item("Dual Land", est_price=100.00, margin=10.00, qty=1)]
    assert _select_within_budget(items, budget_usd=40.00) == []


def test_select_within_budget_sorts_by_total_savings():
    # Item A: margin $1 × qty 1 = $1.00 total savings, cost $5
    # Item B: margin $0.50 × qty 4 = $2.00 total savings, cost $4
    # Budget $4: only one can fit. B has higher total savings → B should be selected.
    items = [
        _cart_item("A", est_price=5.00, margin=1.00, qty=1),
        _cart_item("B", est_price=1.00, margin=0.50, qty=4),
    ]
    selected = _select_within_budget(items, budget_usd=4.00)
    assert len(selected) == 1
    assert selected[0].buy_list_item.card_name == "B"


# ---------------------------------------------------------------------------
# _is_better
# ---------------------------------------------------------------------------

def _make_cart(total_usd: float, net_value_usd: float) -> CartResult:
    return CartResult(
        items=[], raw_cart=[], subtotal_usd=total_usd,
        shipping_usd=0.0, fees_usd=0.0, total_usd=total_usd,
        value_budget_usd=total_usd + net_value_usd, net_value_usd=net_value_usd,
    )


def test_is_better_no_budget_constraint():
    assert _is_better(_make_cart(10, 5), None, None) is True
    a = _make_cart(10, 5)
    b = _make_cart(10, 3)
    assert _is_better(a, b, None) is True
    assert _is_better(b, a, None) is False


def test_is_better_within_budget_prefers_higher_net():
    cap = 20.0
    high_net = _make_cart(15, 10)
    low_net = _make_cart(15, 3)
    assert _is_better(high_net, low_net, cap) is True
    assert _is_better(low_net, high_net, cap) is False


def test_is_better_within_budget_beats_over_budget():
    cap = 20.0
    within = _make_cart(18, 2)
    over = _make_cart(25, 100)  # huge net but over cap
    assert _is_better(within, over, cap) is True
    assert _is_better(over, within, cap) is False


def test_is_better_both_over_budget_prefers_cheaper_total():
    cap = 10.0
    closer = _make_cart(12, 5)
    further = _make_cart(20, 8)
    assert _is_better(closer, further, cap) is True
    assert _is_better(further, closer, cap) is False


# ---------------------------------------------------------------------------
# find_best_cart with max_cart_usd
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_find_best_cart_respects_spending_cap(mp_client):
    """Items that would bust the cap are dropped during pre-selection."""
    _mock_card_ids()
    # Two items: Bolt ($6 estimated) + Dual ($10 estimated) = $16 total
    # Budget $8 → only Bolt fits in pre-selection
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=600, shipping_cents=150, fee_cents=25),
        content_type="application/x-ndjson",
    )

    bolt = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=4), _listing(name="Lightning Bolt", price=1.50))
    dual = _matched(
        _item(name="Dual Land", max_price=15.00, qty=1),
        _listing(name="Dual Land", set_code="LEA", price=10.00, qty=1),
    )
    dual.best_price = 10.00

    cart = find_best_cart([bolt, dual], mp_client, max_cart_usd=8.00)
    assert cart is not None
    assert len(cart.items) == 1
    assert cart.items[0].buy_list_item.card_name == "Lightning Bolt"
    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    assert len(post_calls) == 1  # only baseline, no iteration needed


def test_find_best_cart_returns_none_when_nothing_fits_budget(mp_client):
    dual = _matched(
        _item(name="Dual Land", max_price=15.00, qty=1),
        _listing(name="Dual Land", price=10.00, qty=1),
    )
    dual.best_price = 10.00
    # Budget $5 — $10 item can't fit even at estimated price
    assert find_best_cart([dual], mp_client, max_cart_usd=5.00) is None


@resp_mock.activate
def test_find_best_cart_trims_when_shipping_pushes_over_cap(mp_client):
    """When shipping pushes the optimizer total over the cap, the worst item is removed."""
    _mock_card_ids()
    # effective_cap = $8.00 * 0.80 = $6.40
    # Bolt: $3.00 * 2 = $6.00 ≤ $6.40 → selected.
    # Ritual: $0.10 * 2 = $0.20 → $6.20 ≤ $6.40 → selected.
    # Both items pass greedy selection, but optimizer total $9.00 exceeds cap.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=870, shipping_cents=30, fee_cents=0),  # $9.00 total
        content_type="application/x-ndjson",
    )
    # After removing Ritual (lower total margin), optimizer returns $7.00 (within cap)
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=680, shipping_cents=20, fee_cents=0),  # $7.00 total
        content_type="application/x-ndjson",
    )

    bolt = _matched(_item(name="Lightning Bolt", max_price=5.00, qty=2), _listing(price=3.00))
    ritual = _matched(
        _item(name="Dark Ritual", max_price=1.00, qty=2),
        _listing(name="Dark Ritual", set_code="ICE", price=0.10, qty=2),
    )

    cart = find_best_cart([bolt, ritual], mp_client, max_cart_usd=8.00, max_iterations=3)
    assert cart is not None
    assert cart.total_usd == pytest.approx(7.00)
    assert cart.total_usd <= 8.00


@resp_mock.activate
def test_find_best_cart_survives_phase1_trial_returning_no_valid_cart(mp_client):
    """A failed Phase 1 (budget enforcement) trial must not crash the run -- the seller
    gets locked and find_best_cart still returns the best cart found so far."""
    _mock_card_ids()
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=870, shipping_cents=30, fee_cents=0),  # $9.00 total
        content_type="application/x-ndjson",
    )
    # Removal trial fails outright — stats line only, no cart line.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=json.dumps({"stats": {"response_time": 1}}) + "\n",
        content_type="application/x-ndjson",
    )

    bolt = _matched(_item(name="Lightning Bolt", max_price=5.00, qty=2), _listing(price=3.00))
    ritual = _matched(
        _item(name="Dark Ritual", max_price=1.00, qty=2),
        _listing(name="Dark Ritual", set_code="ICE", price=0.10, qty=2),
    )

    cart = find_best_cart([bolt, ritual], mp_client, max_cart_usd=8.00, max_iterations=3)
    # Falls back to the over-budget baseline rather than crashing.
    assert cart is not None
    assert cart.total_usd == pytest.approx(9.00)


# ---------------------------------------------------------------------------
# find_best_cart: 409 retry
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_find_best_cart_retries_after_409(mp_client):
    """When the baseline 409s, the offending item is removed and the call is retried."""
    _mock_card_ids()
    body_409 = json.dumps({
        "status": 409,
        "message": "Could not find inventory to satisfy request",
        "details": [{"item": {"name": "Stoneforged Blade // Germ", "type": "mtg_single"}}],
    })
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=body_409, status=409, content_type="application/json",
    )
    # Second call (without the unresolvable item) succeeds
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=150, shipping_cents=100, fee_cents=10),
        content_type="application/x-ndjson",
    )

    bad = _matched(
        _item(name="Stoneforged Blade // Germ", max_price=5.00, qty=1),
        _listing(name="Stoneforged Blade // Germ", price=1.00),
    )
    good = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=1), _listing(price=1.00))

    cart = find_best_cart([bad, good], mp_client, preselected=[
        _cart_item("Stoneforged Blade // Germ", est_price=1.00, margin=4.00),
        _cart_item("Lightning Bolt", est_price=1.00, margin=1.00),
    ])
    assert cart is not None
    assert len(cart.items) == 1
    assert cart.items[0].buy_list_item.card_name == "Lightning Bolt"
    assert len(resp_mock.calls) == 2  # retried once


@resp_mock.activate
def test_find_best_cart_returns_none_when_all_items_409(mp_client):
    """If all items are unresolvable, find_best_cart returns None."""
    body_409 = json.dumps({
        "status": 409,
        "message": "Could not find inventory to satisfy request",
        "details": [{"item": {"name": "Lightning Bolt", "type": "mtg_single"}}],
    })
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=body_409, status=409, content_type="application/json",
    )

    result = _matched(_item(name="Lightning Bolt", max_price=2.00, qty=1), _listing(price=1.00))
    cart = find_best_cart([result], mp_client, preselected=[
        _cart_item("Lightning Bolt", est_price=1.00, margin=1.00),
    ])
    assert cart is None


# ---------------------------------------------------------------------------
# Helper: CartRequestItem factory for unit tests
# ---------------------------------------------------------------------------

def _cart_item(
    name: str,
    est_price: float,
    margin: float,
    qty: int = 1,
    set_code: str = "M10",
) -> CartRequestItem:
    return CartRequestItem(
        buy_list_item=BuyListItem(
            card_name=name,
            target_quantity=qty,
            max_price_usd=est_price + margin,
            min_condition=Condition.NM,
        ),
        set_code=set_code,
        estimated_price=est_price,
        estimated_margin=margin,
        condition_ids=["NM"],
        finish_ids=["NF"],
        # Pre-supplied so find_best_cart's card_id resolution treats this item as
        # already resolved and skips the GET /products/singles lookup for it.
        card_id=f"card-{name.lower()}",
    )


def _cart_result(
    items: list[CartRequestItem],
    total_usd: float,
    net_value_usd: float,
) -> CartResult:
    """Build a minimal CartResult for use as the 'current' arg to try_add_items."""
    return CartResult(
        items=items,
        raw_cart=[],
        subtotal_usd=total_usd,
        shipping_usd=0.0,
        fees_usd=0.0,
        total_usd=total_usd,
        value_budget_usd=total_usd + net_value_usd,
        net_value_usd=net_value_usd,
    )


# ---------------------------------------------------------------------------
# try_add_items
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_try_add_items_adds_beneficial_item(mp_client):
    """A candidate that improves net value and stays within budget is added."""
    # existing: max_price=$10, est=$8 → margin $2. value_budget=$10, total=$8.50, net=$1.50
    existing = _cart_item("Lightning Bolt", est_price=8.00, margin=2.00, set_code="M10")
    current = _cart_result([existing], total_usd=8.50, net_value_usd=1.50)

    # candidate: max_price=$3, est=$2 → margin $1
    candidate = _cart_item("Counterspell", est_price=2.00, margin=1.00, set_code="7ED")

    # After adding: value_budget = $10 + $3 = $13. Optimizer returns $11.00.
    # trial.net_value_usd = 13 - 11 = $2.00 > current $1.50 → improvement ✓
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=900, shipping_cents=200, fee_cents=0),  # $11.00
        content_type="application/x-ndjson",
    )

    result = try_add_items(current, [candidate], mp_client, max_cart_usd=20.00)
    assert result.net_value_usd > current.net_value_usd
    assert result.total_usd <= 20.00


@resp_mock.activate
def test_try_add_items_skips_over_budget(mp_client):
    """A candidate is skipped when adding it would push the total over max_cart_usd."""
    existing = _cart_item("Lightning Bolt", est_price=1.50, margin=0.50, set_code="M10")
    current = _cart_result([existing], total_usd=9.50, net_value_usd=2.00)

    candidate = _cart_item("Counterspell", est_price=0.80, margin=1.20, set_code="7ED")

    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=1000, shipping_cents=200, fee_cents=0),  # $12.00
        content_type="application/x-ndjson",
    )

    result = try_add_items(current, [candidate], mp_client, max_cart_usd=10.00)
    assert result is current  # unchanged
    assert len(result.items) == 1


@resp_mock.activate
def test_try_add_items_skips_no_improvement(mp_client):
    """A candidate is skipped when it does not improve net value (extra shipping eats margin)."""
    existing = _cart_item("Lightning Bolt", est_price=1.50, margin=0.50, set_code="M10")
    current = _cart_result([existing], total_usd=8.50, net_value_usd=2.00)

    candidate = _cart_item("Counterspell", est_price=0.80, margin=0.20, set_code="7ED")

    # Optimizer returns worse net (e.g. new seller adds $1.50 shipping)
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=950, shipping_cents=300, fee_cents=50),  # $13.00 total
        content_type="application/x-ndjson",
    )

    result = try_add_items(current, [candidate], mp_client)
    # net_value on trial = value_budget_usd - 13.00
    # value_budget_usd = (1.50+0.50)*4 + (0.80+0.20)*1 = 8+1 = 9 -> net = 9 - 13 = -4
    # current net = 2.00, so trial is worse -> skipped
    assert result is current


def test_try_add_items_returns_current_when_no_candidates(mp_client):
    """Returns current unchanged when candidates list is empty."""
    existing = _cart_item("Lightning Bolt", est_price=1.50, margin=0.50, set_code="M10")
    current = _cart_result([existing], total_usd=8.50, net_value_usd=2.00)
    assert try_add_items(current, [], mp_client) is current


# ---------------------------------------------------------------------------
# _group_by_seller
# ---------------------------------------------------------------------------

def test_group_by_seller_groups_same_seller():
    a = _cart_item("Card A", est_price=1.00, margin=0.50)
    a.seller_id = "seller_x"
    b = _cart_item("Card B", est_price=2.00, margin=1.00)
    b.seller_id = "seller_x"
    c = _cart_item("Card C", est_price=1.50, margin=2.00)
    c.seller_id = "seller_y"

    groups = _group_by_seller([a, b, c])
    assert len(groups) == 2
    seller_map = {k: [x.buy_list_item.card_name for x in v] for k, v in groups}
    assert set(seller_map["seller_x"]) == {"Card A", "Card B"}
    assert seller_map["seller_y"] == ["Card C"]


def test_group_by_seller_sorted_worst_first():
    """Seller with lower total gross margin comes first."""
    a = _cart_item("Card A", est_price=1.00, margin=0.10)
    a.seller_id = "low_margin_seller"
    b = _cart_item("Card B", est_price=1.00, margin=5.00)
    b.seller_id = "high_margin_seller"

    groups = _group_by_seller([a, b])
    assert groups[0][0] == "low_margin_seller"
    assert groups[1][0] == "high_margin_seller"


def test_group_by_seller_unknown_seller_singleton():
    """Items without seller_id each get their own singleton group."""
    a = _cart_item("Card A", est_price=1.00, margin=0.50)  # seller_id=""
    b = _cart_item("Card B", est_price=1.00, margin=0.50)  # seller_id=""
    groups = _group_by_seller([a, b])
    assert len(groups) == 2  # each item in its own group


# ---------------------------------------------------------------------------
# find_best_cart: forced_card_names
# ---------------------------------------------------------------------------

@resp_mock.activate
def test_forced_card_included_despite_over_budget_pct_filter(mp_client):
    """A forced card bypasses the over_budget_pct price filter and is always eligible."""
    _mock_card_ids()
    # Counterspell: max_price=$1.00, listing=$1.50 — 50% over max → normally filtered out.
    # With forced, it must appear in the initial eligible set and in the final cart.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=150, shipping_cents=100, fee_cents=0),
        content_type="application/x-ndjson",
    )

    counter = _matched(
        _item(name="Counterspell", max_price=1.00, qty=1),
        _listing(name="Counterspell", set_code="7ED", price=1.50, qty=1),
    )
    counter.best_price = 1.50

    # over_budget_pct=0 would normally exclude Counterspell (1.50 > 1.00).
    cart = find_best_cart(
        [counter], mp_client,
        max_cart_usd=10.00,
        over_budget_pct=0.0,
        forced_card_names=frozenset({"Counterspell"}),
    )
    assert cart is not None
    assert cart.items[0].buy_list_item.card_name == "Counterspell"


@resp_mock.activate
def test_forced_card_not_removed_in_phase2_only_forced_item(mp_client):
    """When the only item is forced, Phase 2 has no candidates and makes no removal trials."""
    _mock_card_ids()
    # Counterspell is forced and is the only item.
    # Phase 2 filters out forced items → candidates=[] → loop breaks immediately.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=150, shipping_cents=100, fee_cents=0),
        content_type="application/x-ndjson",
    )

    counter = _matched(
        _item(name="Counterspell", max_price=1.00, qty=1),
        _listing(name="Counterspell", set_code="7ED", price=1.50, qty=1),
    )
    counter.best_price = 1.50

    cart = find_best_cart(
        [counter], mp_client,
        max_cart_usd=10.00,
        max_iterations=3,
        forced_card_names=frozenset({"Counterspell"}),
    )
    post_calls = [c for c in resp_mock.calls if c.request.method == "POST"]
    assert len(post_calls) == 1  # baseline only — no Phase 2 trials
    assert cart is not None
    assert cart.items[0].buy_list_item.card_name == "Counterspell"


@resp_mock.activate
def test_forced_card_cost_deducted_from_optional_budget(mp_client):
    """Forced card's estimated cost is deducted from the optional build budget."""
    _mock_card_ids()
    # max_cart_usd=$10 → build_budget=$8 (×0.80). Forced Dual=$5 → optional_budget=$3.
    # Bolt costs $4/ea × 1 = $4 > $3 → Bolt excluded from initial selection.
    # The optimizer baseline runs with only the forced Dual Land.
    # Use separate seller IDs so Bolt doesn't get added as a Phase-3 free rider.
    resp_mock.add(
        resp_mock.POST, f"{MANAPOOL_BASE}/buyer/optimizer",
        body=_optimizer_ndjson(subtotal_cents=500, shipping_cents=200, fee_cents=0),
        content_type="application/x-ndjson",
    )

    bolt_listing = PriceListing(
        scryfall_id="bolt-id", card_name="Lightning Bolt", set_code="M10",
        condition=Condition.NM, finish=Finish.NONFOIL, price_usd=4.00,
        quantity_available=1, seller_id="seller_bolt",
        fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    dual_listing = PriceListing(
        scryfall_id="dual-id", card_name="Dual Land", set_code="LEA",
        condition=Condition.NM, finish=Finish.NONFOIL, price_usd=5.00,
        quantity_available=1, seller_id="seller_dual",
        fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    bolt = _matched(_item(name="Lightning Bolt", max_price=6.00, qty=1), bolt_listing)
    dual = _matched(_item(name="Dual Land", max_price=8.00, qty=1), dual_listing)
    bolt.best_price = 4.00
    dual.best_price = 5.00

    cart = find_best_cart(
        [bolt, dual], mp_client,
        max_cart_usd=10.00,
        forced_card_names=frozenset({"Dual Land"}),
    )
    assert cart is not None
    # Bolt squeezed out of initial selection; Dual forced into cart.
    # Bolt would be in _overflow (free rider pool) but has a different seller → Phase 3 skips it.
    # Bolt also a new-seller candidate for Phase 4, but no mock registered for that call.
    # The cart contains at minimum the forced Dual Land.
    names = {x.buy_list_item.card_name for x in cart.items}
    assert "Dual Land" in names


# ---------------------------------------------------------------------------
# _chunk
# ---------------------------------------------------------------------------

def test_chunk_splits_evenly():
    assert _chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_chunk_handles_remainder():
    assert _chunk([1, 2, 3, 4, 5], 2) == [[1, 2, 3], [4, 5]]


def test_chunk_caps_at_item_count():
    """Asking for more chunks than items just gives one item per chunk."""
    assert _chunk([1, 2], 10) == [[1], [2]]


def test_chunk_empty_input():
    assert _chunk([], 4) == []


# ---------------------------------------------------------------------------
# _try_batch
# ---------------------------------------------------------------------------

def test_try_batch_returns_none_when_removing_everything():
    ci = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    client = MagicMock()
    assert _try_batch([ci], [ci], client, {"model": "lowest_price", "destination": "US"}) is None
    client.run_optimizer.assert_not_called()


def test_try_batch_returns_none_on_api_error():
    ci_keep = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    ci_remove = _cart_item("Dark Ritual", est_price=1.00, margin=1.00)
    client = MagicMock()
    client.run_optimizer.side_effect = ManaPoolAPIError("boom")
    result = _try_batch([ci_remove], [ci_keep, ci_remove], client, {"model": "lowest_price", "destination": "US"})
    assert result is None


# ---------------------------------------------------------------------------
# _bisect_batch_removal
# ---------------------------------------------------------------------------

class _FakeOptimizerClient:
    """Deterministic stand-in for ManaPoolClient.run_optimizer(), for testing
    _bisect_batch_removal's control flow without any HTTP mocking. penalty_if_absent
    simulates a card that quietly earns shipping-consolidation credit -- removing it
    makes every other trial that also lacks it worse, even though its own margin looks bad.
    """
    def __init__(self, base_prices, penalty_if_absent=None, fail_names=None):
        self.base_prices = base_prices
        self.penalty_if_absent = penalty_if_absent or {}
        self.fail_names = fail_names or set()
        self.call_count = 0
        self.thread_ids: set = set()

    def run_optimizer(self, cart, **kwargs):
        self.call_count += 1
        self.thread_ids.add(threading.get_ident())
        names = [entry["name"] for entry in cart]
        if any(n in self.fail_names for n in names):
            raise ManaPoolAPIError("simulated failure")
        subtotal = sum(self.base_prices.get(n, 1.0) for n in names)
        penalty = sum(v for k, v in self.penalty_if_absent.items() if k not in names)
        return {
            "cart": [{"inventory_id": f"inv-{n}", "quantity_selected": 1} for n in names],
            "totals": {
                "subtotal_cents": round(subtotal * 100),
                "shipping_cents": round(penalty * 100),
                "buyer_fee_cents": 0,
                "total_cents": round((subtotal + penalty) * 100),
            },
        }


def _net_value_accept(trial: CartResult, baseline: CartResult) -> bool:
    return trial.net_value_usd >= baseline.net_value_usd


def _run_kwargs_for_fake():
    return {"model": "lowest_price", "destination": "US"}


def test_bisect_batch_removal_no_candidates_is_a_noop():
    ci = _cart_item("Lightning Bolt", est_price=1.00, margin=1.00)
    baseline = _cart_result([ci], total_usd=1.00, net_value_usd=1.00)
    client = MagicMock()
    current, result, removed = _bisect_batch_removal(
        [], [ci], baseline, client, {}, _net_value_accept,
    )
    assert current == [ci]
    assert result is baseline
    assert removed == []
    client.run_optimizer.assert_not_called()


def test_bisect_batch_removal_validates_each_group_before_merging():
    """All candidates are unambiguously bad and removing them together improves net
    value with no interaction effects -- resolves via one round of small-group probing
    (each item checked standalone) plus one merge-validate call, deliberately never a
    single blind whole-batch call (see _bisect_batch_removal's docstring: a large batch
    passing in aggregate doesn't prove every item in it individually deserved removal)."""
    good = _cart_item("Good Card", est_price=1.00, margin=4.00)  # max_price implied 5.00
    bad1 = _cart_item("Bad One", est_price=2.00, margin=-1.00)   # max_price implied 1.00
    bad2 = _cart_item("Bad Two", est_price=2.00, margin=-1.00)

    client = _FakeOptimizerClient(base_prices={"Good Card": 1.00, "Bad One": 2.00, "Bad Two": 2.00})
    baseline_raw = client.run_optimizer(_build_optimizer_payload([good, bad1, bad2]))
    from manabot.optimizer import _score
    baseline_result = _score([good, bad1, bad2], baseline_raw)
    client.call_count = 0  # don't count the baseline call above against the assertion below

    current, result, removed = _bisect_batch_removal(
        [bad1, bad2], [good, bad1, bad2], baseline_result, client, _run_kwargs_for_fake(), _net_value_accept,
    )

    # 2 standalone probes (Bad One alone, Bad Two alone) + 1 merge-validate call.
    assert client.call_count == 3
    assert {x.buy_list_item.card_name for x in removed} == {"Bad One", "Bad Two"}
    assert [x.buy_list_item.card_name for x in current] == ["Good Card"]
    assert result.net_value_usd > baseline_result.net_value_usd


def test_bisect_batch_removal_isolates_shipping_consolidation_item():
    """Whole-batch removal fails because one candidate (C4) is quietly earning
    shipping-consolidation credit. Parallel probing should find that C1-C3 are safe to
    remove together, validate that combination in one merge call, then correctly keep
    C4 despite its own negative margin -- matching what the old sequential algorithm
    would conclude, just via batched/parallel calls instead of one-at-a-time."""
    good = _cart_item("Good Card", est_price=1.00, margin=4.00)  # max_price 5.00
    c1 = _cart_item("C1", est_price=2.00, margin=-1.00)          # max_price 1.00
    c2 = _cart_item("C2", est_price=2.00, margin=-1.00)
    c3 = _cart_item("C3", est_price=2.00, margin=-1.00)
    c4 = _cart_item("C4", est_price=1.50, margin=-0.50)          # max_price 1.00

    client = _FakeOptimizerClient(
        base_prices={"Good Card": 1.00, "C1": 2.00, "C2": 2.00, "C3": 2.00, "C4": 1.50},
        penalty_if_absent={"C4": 5.00},
    )
    baseline_raw = client.run_optimizer(_build_optimizer_payload([good, c1, c2, c3, c4]))
    from manabot.optimizer import _score
    baseline_result = _score([good, c1, c2, c3, c4], baseline_raw)
    client.call_count = 0

    current, result, removed = _bisect_batch_removal(
        [c1, c2, c3, c4], [good, c1, c2, c3, c4], baseline_result,
        client, _run_kwargs_for_fake(), _net_value_accept,
    )

    removed_names = {x.buy_list_item.card_name for x in removed}
    current_names = {x.buy_list_item.card_name for x in current}
    assert removed_names == {"C1", "C2", "C3"}
    assert current_names == {"Good Card", "C4"}
    assert result.net_value_usd > baseline_result.net_value_usd
    # Sanity: this took meaningfully fewer calls than one-per-candidate would for a
    # bigger cart, and used more than one thread (the parallel probe actually ran
    # concurrently, not silently degraded to sequential).
    assert client.call_count <= 7
    assert len(client.thread_ids) > 1


def test_bisect_batch_removal_all_candidates_stay_when_nothing_helps():
    """If removing anything (alone or together) makes things worse, everything is kept
    and nothing is reported as removed."""
    good = _cart_item("Good Card", est_price=1.00, margin=4.00)
    c1 = _cart_item("C1", est_price=1.00, margin=4.00)  # actually fine, "candidate" only nominally

    # Any removal increases total (bad) -- simulate via a heavy shared penalty that
    # applies whenever either item is missing.
    client = _FakeOptimizerClient(
        base_prices={"Good Card": 1.00, "C1": 1.00},
        penalty_if_absent={"Good Card": 10.0, "C1": 10.0},
    )
    baseline_raw = client.run_optimizer(_build_optimizer_payload([good, c1]))
    from manabot.optimizer import _score
    baseline_result = _score([good, c1], baseline_raw)
    client.call_count = 0

    current, result, removed = _bisect_batch_removal(
        [c1], [good, c1], baseline_result, client, _run_kwargs_for_fake(), _net_value_accept,
    )
    assert removed == []
    assert {x.buy_list_item.card_name for x in current} == {"Good Card", "C1"}


def test_bisect_batch_removal_treats_api_failure_as_rejection():
    good = _cart_item("Good Card", est_price=1.00, margin=4.00)
    flaky = _cart_item("Flaky Card", est_price=2.00, margin=-1.00)

    client = _FakeOptimizerClient(base_prices={"Good Card": 1.00, "Flaky Card": 2.00})

    # Make the only possible trial (removing Flaky) always fail, simulating a
    # ManaPoolAPIError on that specific combination.
    def _always_fail(cart, **kwargs):
        raise ManaPoolAPIError("simulated failure")

    client.run_optimizer = _always_fail
    baseline = _cart_result([good, flaky], total_usd=3.00, net_value_usd=2.00)

    current, result, removed = _bisect_batch_removal(
        [flaky], [good, flaky], baseline, client, _run_kwargs_for_fake(), _net_value_accept,
    )
    # The only trial possible (removing Flaky) always fails -> treated as rejected, kept.
    assert removed == []
    assert {x.buy_list_item.card_name for x in current} == {"Good Card", "Flaky Card"}
