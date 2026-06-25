"""Cart value optimization using the ManaPool /buyer/optimizer endpoint.

Goal: maximize net value (Σ max_price_i × qty_i  −  total_cart_cost) by finding
the best subset of eligible buy-list items to send in a single optimizer run.

Scoring
-------
    value_budget  = Σ(buy_list_item.max_price_usd × target_quantity)
    net_value     = value_budget − optimizer_total_cost_usd

    A cart is profitable when net_value > 0 (buying below collective valuation).

Iteration
---------
    1.  Build eligible items: estimated_price ≤ max_price × (1 + over_budget_pct%).
    2.  If max_cart_usd is set, greedily pre-select the highest-value items that fit
        within that estimated dollar cap (sorted by total savings = margin × qty).
    3.  Run optimizer → baseline result.
    4.  Iterate up to max_iterations times:
        a.  If cart total > max_cart_usd: force-remove the worst-margin item to get
            closer to budget.
        b.  Else if any negative-margin items remain: try removing the worst; keep
            removal only if net value improves.
        c.  Stop when within budget with no negative-margin removal opportunities.

Total API calls: 1 (baseline) + up to max_iterations (one removal trial each).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manabot.api.scryfall_bulk import ScryfallBulk

from manabot.models import (
    CartRequestItem,
    CartResult,
    Condition,
    Finish,
    MatchResult,
    MatchStatus,
    _CONDITION_RANK,
)
from manabot.api.manapool import ManaPool409Error, ManaPoolClient

log = logging.getLogger(__name__)

_CONDITIONS_BEST_FIRST: list[Condition] = [
    Condition.NM, Condition.LP, Condition.MP, Condition.HP, Condition.DMG
]

_FINISH_IDS: dict[Finish, list[str]] = {
    Finish.NONFOIL: ["NF"],
    Finish.FOIL: ["FO"],
    Finish.ANY: ["NF", "FO"],
}


def _acceptable_conditions(min_condition: Condition) -> list[str]:
    """Return ManaPool condition IDs at or better than min_condition."""
    return [
        c.value for c in _CONDITIONS_BEST_FIRST
        if _CONDITION_RANK[c] >= _CONDITION_RANK[min_condition]
    ]


def build_request_items(
    match_results: list[MatchResult],
    over_budget_pct: float = 0.0,
    scryfall: "ScryfallBulk | None" = None,
) -> list[CartRequestItem]:
    """Convert match results into optimizer request items.

    Only MATCHED results are included. When scryfall is provided, listings from
    non-playable sets (memorabilia, funny, token) are excluded so the estimated
    price reflects a sanctioned printing. Items whose estimated price exceeds
    max_price × (1 + over_budget_pct/100) are excluded.
    """
    items: list[CartRequestItem] = []
    for result in match_results:
        if result.status != MatchStatus.MATCHED or not result.listings or result.best_price is None:
            continue

        item = result.buy_list_item

        listings = result.listings
        if scryfall is not None:
            listings = [l for l in listings if scryfall.is_playable_set(l.set_code)]
        if not listings:
            log.debug("No playable-set listings for %r — skipping", item.card_name)
            continue

        best_listing = min(listings, key=lambda l: l.price_usd)
        best_price = best_listing.price_usd

        threshold = item.max_price_usd * (1.0 + over_budget_pct / 100.0)
        if best_price > threshold:
            log.debug(
                "Excluding %r: best playable-set price $%.2f exceeds threshold $%.2f",
                item.card_name, best_price, threshold,
            )
            continue

        items.append(CartRequestItem(
            buy_list_item=item,
            set_code=best_listing.set_code,
            estimated_price=best_price,
            estimated_margin=item.max_price_usd - best_price,
            condition_ids=_acceptable_conditions(item.min_condition),
            finish_ids=_FINISH_IDS[item.foil],
            seller_id=best_listing.seller_id,
        ))

    return items


def _select_within_budget(
    items: list[CartRequestItem],
    max_cart_usd: float,
) -> list[CartRequestItem]:
    """Greedily select the best items that fit within max_cart_usd (estimated prices).

    Items are sorted by total estimated savings (margin × qty) descending so the
    most valuable items are prioritised. Shipping and fees are excluded from this
    estimate — the iteration step corrects for any overage after the optimizer runs.
    """
    # Reserve 20% of the cap for shipping and fees so the optimizer's actual total
    # stays within budget. Estimated prices often understate final cart cost.
    effective_cap = max_cart_usd * 0.80
    sorted_items = sorted(
        items,
        key=lambda x: x.estimated_margin * x.buy_list_item.target_quantity,
        reverse=True,
    )
    selected: list[CartRequestItem] = []
    running_total = 0.0
    for item in sorted_items:
        item_cost = item.estimated_price * item.buy_list_item.target_quantity
        if running_total + item_cost <= effective_cap:
            selected.append(item)
            running_total += item_cost
    return selected


def _group_by_seller(
    items: list[CartRequestItem],
) -> list[tuple[str, list[CartRequestItem]]]:
    """Group items by seller, sorted by ascending total estimated margin (worst first).

    Items without a seller_id each get a unique singleton key so they fall back to
    per-item removal rather than all collapsing into one unknown-seller group.
    Returns [(seller_key, items)] with the lowest-gross-margin seller first.
    """
    groups: dict[str, list[CartRequestItem]] = {}
    for item in items:
        key = item.seller_id if item.seller_id else f"__solo_{id(item)}"
        groups.setdefault(key, []).append(item)
    return sorted(
        groups.items(),
        key=lambda kv: sum(x.estimated_margin for x in kv[1]),
    )


def _is_better(
    new: CartResult,
    current_best: CartResult | None,
    max_cart_usd: float | None,
) -> bool:
    """True if new should replace current_best as the best cart seen so far."""
    if current_best is None:
        return True
    if max_cart_usd is None:
        return new.net_value_usd > current_best.net_value_usd

    new_ok = new.total_usd <= max_cart_usd
    best_ok = current_best.total_usd <= max_cart_usd

    if new_ok and best_ok:
        return new.net_value_usd > current_best.net_value_usd
    if new_ok and not best_ok:
        return True   # new is within budget; current isn't
    if not new_ok and best_ok:
        return False  # current is within budget; new isn't
    return new.total_usd < current_best.total_usd  # both over budget: prefer cheaper


def _build_optimizer_payload(items: list[CartRequestItem]) -> list[dict]:
    payload = []
    for item in items:
        entry: dict = {
            "type": "mtg_single",
            "name": item.buy_list_item.card_name,
            "is_token": False,
            "include_non_sanctioned_legal": False,  # excludes WC04, CE, etc.
            "language_ids": ["EN"],
            "condition_ids": item.condition_ids,
            "finish_ids": item.finish_ids,
            "quantity_requested": item.buy_list_item.target_quantity,
        }
        # Only constrain set_code when the user explicitly specified allowed_sets,
        # so the optimizer can still find the cheapest printing across all sanctioned sets.
        if item.buy_list_item.allowed_sets:
            entry["set_code"] = item.set_code
        payload.append(entry)
    return payload


def _score(items: list[CartRequestItem], raw: dict) -> CartResult:
    totals = raw.get("totals", {})
    subtotal = totals.get("subtotal_cents", 0) / 100.0
    shipping = totals.get("shipping_cents", 0) / 100.0
    fees = totals.get("buyer_fee_cents", 0) / 100.0
    total = totals.get("total_cents", 0) / 100.0

    value_budget = sum(
        x.buy_list_item.max_price_usd * x.buy_list_item.target_quantity
        for x in items
    )

    return CartResult(
        items=items,
        raw_cart=raw.get("cart", []),
        subtotal_usd=subtotal,
        shipping_usd=shipping,
        fees_usd=fees,
        total_usd=total,
        value_budget_usd=value_budget,
        net_value_usd=value_budget - total,
    )


def _run_single(
    items: list[CartRequestItem],
    client: ManaPoolClient,
    model: str,
    destination: str,
    exclude_universes_beyond: bool = False,
    exclude_preorder: bool = False,
) -> CartResult:
    payload = _build_optimizer_payload(items)
    raw = client.run_optimizer(
        payload,
        model=model,
        destination_country=destination,
        exclude_universes_beyond=exclude_universes_beyond,
        exclude_preorder=exclude_preorder,
    )
    # Log a raw cart item sample at DEBUG so we can discover any undocumented seller fields.
    if log.isEnabledFor(logging.DEBUG) and raw.get("cart"):
        log.debug("Raw optimizer cart item (sample): %s", raw["cart"][0])
    return _score(items, raw)


def find_best_cart(
    match_results: list[MatchResult],
    client: ManaPoolClient,
    over_budget_pct: float = 0.0,
    max_cart_usd: float | None = None,
    max_iterations: int = 5,
    optimizer_model: str = "lowest_price",
    destination_country: str = "US",
    preselected: list[CartRequestItem] | None = None,
    scryfall: "ScryfallBulk | None" = None,
    exclude_preorder: bool = False,
) -> CartResult | None:
    """Find the cart configuration that maximizes net value.

    When max_cart_usd is set, the result's total (including shipping and fees)
    will not exceed that limit if at all possible. Items are pre-selected by
    estimated savings, then the iteration enforces the hard limit on the real total.

    Pass preselected to bypass build_request_items and _select_within_budget
    (e.g. when the caller has already done greedy budget packing for arbitrage).

    Returns None if no matched results are eligible. Makes at most
    1 + max_iterations optimizer API calls.
    """
    if preselected is not None:
        eligible = list(preselected)
    else:
        eligible = build_request_items(match_results, over_budget_pct, scryfall=scryfall)
        if not eligible:
            log.warning("No eligible items for cart optimization")
            return None

        if max_cart_usd is not None:
            eligible = _select_within_budget(eligible, max_cart_usd)
            if not eligible:
                log.warning(
                    "No items fit within the $%.2f budget at estimated prices", max_cart_usd
                )
                return None

    log.info("Starting cart optimization: %d eligible items", len(eligible))

    # Baseline with 409 retry: some items may not exist in the optimizer index
    # (name mismatches, non-sanctioned-set printings, token DFCs, etc.).
    # Remove each unresolvable item and retry until the baseline succeeds or nothing remains.
    exclude_ub = any(r.buy_list_item.exclude_ub for r in match_results)
    _run_kwargs = dict(
        model=optimizer_model,
        destination=destination_country,
        exclude_universes_beyond=exclude_ub,
        exclude_preorder=exclude_preorder,
    )
    current = eligible
    for _ in range(len(eligible)):
        try:
            current_result = _run_single(current, client, **_run_kwargs)
            break
        except ManaPool409Error as e:
            if not e.unresolvable_names:
                raise
            name_set = set(e.unresolvable_names)
            for name in e.unresolvable_names:
                log.warning("Skipping %r — not found in optimizer index (409)", name)
            current = [x for x in current if x.buy_list_item.card_name not in name_set]
            if not current:
                log.warning("No items remain after removing unresolvable items")
                return None
    else:
        log.warning("Could not establish a baseline cart after repeated 409 errors")
        return None

    best: CartResult | None = current_result if _is_better(current_result, None, max_cart_usd) else None

    log.info(
        "Baseline: %d items, budget $%.2f, cart $%.2f "
        "(sub $%.2f + ship $%.2f + fees $%.2f), net $%+.2f",
        len(current), current_result.value_budget_usd, current_result.total_usd,
        current_result.subtotal_usd, current_result.shipping_usd,
        current_result.fees_usd, current_result.net_value_usd,
    )

    # Log per-seller breakdown so shipping concentration is visible.
    seller_groups = _group_by_seller(current)
    named_sellers = [(k, g) for k, g in seller_groups if not k.startswith("__solo_")]
    if named_sellers:
        avg_shipping = (
            current_result.shipping_usd / len(named_sellers) if named_sellers else 0.0
        )
        log.info(
            "Seller analysis: %d seller(s), avg $%.2f shipping/package — "
            "Phase 1 will remove lowest-gross-margin packages first",
            len(named_sellers), avg_shipping,
        )
        for key, grp in seller_groups:
            if key.startswith("__solo_"):
                continue
            gross = sum(x.estimated_margin for x in grp)
            cost = sum(x.estimated_price for x in grp)
            log.debug(
                "  seller %-24s  %2d item(s)  cost $%6.2f  gross $%+6.2f",
                key, len(grp), cost, gross,
            )

    locked: set[int] = set()

    # Phase 1: Budget enforcement — remove seller packages until under cap.
    # Each iteration removes the entire package from the lowest-gross-margin seller.
    # Removing a full package eliminates that seller's shipping overhead at once,
    # reducing the problem from O(N_items) removals to O(N_sellers).
    # Items without a seller_id are each treated as their own singleton so they fall
    # back to per-item behavior rather than collapsing into one giant unknown group.
    if max_cart_usd is not None:
        locked_sellers: set[str] = set()
        for _ in range(len(current) + 1):
            if current_result.total_usd <= max_cart_usd:
                break

            ranked = [
                (key, grp) for key, grp in _group_by_seller(current)
                if key not in locked_sellers
            ]
            if not ranked:
                log.warning(
                    "All seller packages exhausted, still over budget ($%.2f > $%.2f). "
                    "No valid cart found.",
                    current_result.total_usd, max_cart_usd,
                )
                return best

            worst_key, worst_grp = ranked[0]
            worst_ids = {id(x) for x in worst_grp}
            trial_set = [x for x in current if id(x) not in worst_ids]

            if not trial_set:
                if len(worst_grp) == 1:
                    log.warning(
                        "Only seller %r remains at $%.2f — still exceeds cap $%.2f.",
                        worst_key, current_result.total_usd, max_cart_usd,
                    )
                    return best
                # Multiple items, single seller: can't drop the whole package.
                # Fall back to removing the worst individual item within the group.
                worst_in_grp = min(worst_grp, key=lambda x: x.estimated_margin)
                trial_set = [x for x in current if x is not worst_in_grp]

            trial = _run_single(trial_set, client, **_run_kwargs)
            gross = sum(x.estimated_margin for x in worst_grp)
            if trial.total_usd < current_result.total_usd:
                log.info(
                    "Removed seller %r (%d item(s), est. gross $%.2f) "
                    "— total $%.2f → $%.2f",
                    worst_key, len(worst_grp), gross,
                    current_result.total_usd, trial.total_usd,
                )
                current = trial_set
                current_result = trial
                if _is_better(trial, best, max_cart_usd):
                    best = trial
            else:
                log.info(
                    "Kept seller %r (%d item(s), est. gross $%.2f) "
                    "— removing them did not reduce total cost (shipping consolidation)",
                    worst_key, len(worst_grp), gross,
                )
                locked_sellers.add(worst_key)
        else:
            log.warning("Budget enforcement loop exhausted without reaching cap.")

    # Phase 2: Value optimization — remove negative-margin items up to max_iterations.
    for iteration in range(max_iterations):
        candidates = [x for x in current if id(x) not in locked]
        if not candidates:
            break

        worst = min(candidates, key=lambda x: x.estimated_margin)
        trial_set = [x for x in current if x is not worst]
        if not trial_set:
            locked.add(id(worst))
            continue

        trial = _run_single(trial_set, client, **_run_kwargs)
        log.debug(
            "Opt iteration %d: without %r ($%+.2f margin) → total $%.2f, net $%+.2f",
            iteration + 1, worst.buy_list_item.card_name, worst.estimated_margin,
            trial.total_usd, trial.net_value_usd,
        )

        if trial.net_value_usd >= current_result.net_value_usd:
            log.info(
                "Removed %r (margin $%+.2f) — net value %s ($%.2f → $%.2f)",
                worst.buy_list_item.card_name, worst.estimated_margin,
                "improved" if trial.net_value_usd > current_result.net_value_usd else "unchanged",
                current_result.net_value_usd, trial.net_value_usd,
            )
            current = trial_set
            current_result = trial
            if _is_better(trial, best, max_cart_usd):
                best = trial
        else:
            log.info(
                "Kept %r — shipping consolidation worth $%.2f",
                worst.buy_list_item.card_name,
                current_result.net_value_usd - trial.net_value_usd,
            )
            locked.add(id(worst))

    return best


def try_add_items(
    current: CartResult,
    candidates: list[CartRequestItem],
    client: ManaPoolClient,
    max_cart_usd: float | None = None,
    optimizer_model: str = "lowest_price",
    destination_country: str = "US",
    exclude_preorder: bool = False,
) -> CartResult:
    """Try adding overflow items to an existing cart, keeping improvements.

    Each candidate is tried; it is kept only when it strictly improves net_value_usd
    AND the resulting total stays within max_cart_usd. Intended for the "free-rider"
    case where candidates come from sellers already in the cart — their shipping is
    already paid, so any positive-margin card from that seller is pure upside.

    Candidates should be pre-filtered and pre-sorted by the caller (e.g. seller already
    in cart, ordered by discount% descending). Returns at least current.
    """
    best = current
    for candidate in candidates:
        trial_items = list(best.items) + [candidate]
        try:
            trial = _run_single(
                trial_items, client,
                model=optimizer_model,
                destination=destination_country,
                exclude_universes_beyond=False,  # arb additions allow UB by default
                exclude_preorder=exclude_preorder,
            )
        except ManaPool409Error:
            log.debug(
                "Free-rider %r not in optimizer index (409) — skipping",
                candidate.buy_list_item.card_name,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("Free-rider %r skipped: %s", candidate.buy_list_item.card_name, exc)
            continue

        if max_cart_usd is not None and trial.total_usd > max_cart_usd:
            log.debug(
                "Free-rider %r skipped — total $%.2f > cap $%.2f",
                candidate.buy_list_item.card_name, trial.total_usd, max_cart_usd,
            )
            continue

        if trial.net_value_usd > best.net_value_usd:
            log.info(
                "Added free-rider %r (seller %r, est. margin $%.2f) — net $%.2f → $%.2f",
                candidate.buy_list_item.card_name,
                candidate.seller_id or "(scan seller unknown)",
                candidate.estimated_margin,
                best.net_value_usd,
                trial.net_value_usd,
            )
            best = trial
        else:
            log.debug(
                "Free-rider %r skipped — net $%.2f → $%.2f (no improvement)",
                candidate.buy_list_item.card_name, best.net_value_usd, trial.net_value_usd,
            )

    return best
