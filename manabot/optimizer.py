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
    2.  Resolve each item's ManaPool card_id via GET /products/singles (batched, keyed by
        scryfall_id) — required identifier on every optimizer cart item as of ~2026-08;
        items with no match are dropped rather than sent unidentified.
    3.  If max_cart_usd is set, greedily pre-select the highest-value items that fit
        within that estimated dollar cap (sorted by relative margin rate × qty).
    4.  Run optimizer → baseline result.
    5.  Phase 1 (if max_cart_usd set): remove seller packages, worst-gross-margin first,
        until under budget.
    6.  Phase 2a: batch-remove every negative-margin item via _bisect_batch_removal —
        each item is validated standalone in a small parallel group before being
        merged into the actual removal, never swept out in one blind whole-batch call.
    7.  Phase 2b: same batch/bisect treatment for whatever's left (positive-margin but
        marginal items), as its own pass — separate from 2a since mixing profitable
        items in would make most of 2a's groups fail to improve net value. Neither
        pass is bounded by max_iterations — batching makes an iteration cap
        unnecessary here, unlike the one-at-a-time loop this replaced.

Total API calls: 1 (card_id resolution, batched at 100/call) + 1 (baseline)
                + Phase 1 removals + Phase 2 (O(n / max_parallel) rounds, not one-per-item)
                + Phase 3/4 (≤ max_iterations new-seller probes).
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Callable, TYPE_CHECKING

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
from manabot.api.manapool import ManaPool409Error, ManaPoolAPIError, ManaPoolClient

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
            scryfall_id=best_listing.scryfall_id,
        ))

    return items


def _resolve_card_ids(items: list[CartRequestItem], client: ManaPoolClient) -> list[CartRequestItem]:
    """Resolve card_id (ManaPool's required optimizer identifier) via scryfall_id, batched
    in one call, for any item that doesn't already have one. Callers may pre-supply
    card_id directly (e.g. arbitrage candidates already resolved elsewhere) to skip the
    lookup for those items. Items that end up with no card_id are dropped (logged as a
    warning) rather than sent unidentified, since one bad item 400s the whole request.
    """
    if not items:
        return items
    need_lookup = [x for x in items if not x.card_id and x.scryfall_id]
    if need_lookup:
        card_ids = client.get_card_ids_by_scryfall_id([x.scryfall_id for x in need_lookup])
        for item in need_lookup:
            item.card_id = card_ids.get(item.scryfall_id, "")

    resolved: list[CartRequestItem] = []
    for item in items:
        if not item.card_id:
            log.warning(
                "No ManaPool card_id for %r (scryfall_id=%r) — skipping (optimizer requires an identifier per item)",
                item.buy_list_item.card_name, item.scryfall_id,
            )
            continue
        resolved.append(item)
    return resolved


def _select_within_budget(
    items: list[CartRequestItem],
    budget_usd: float,
) -> list[CartRequestItem]:
    """Greedily select the best items that fit within budget_usd (estimated prices).

    Items are sorted by relative margin rate (margin / price × qty) descending so
    the best-deal-per-dollar items are prioritised over high-price low-percentage
    discounts. Shipping and fees are excluded — the caller is responsible for headroom.
    """
    sorted_items = sorted(
        items,
        key=lambda x: (x.estimated_margin / x.estimated_price) * x.buy_list_item.target_quantity,
        reverse=True,
    )
    selected: list[CartRequestItem] = []
    running_total = 0.0
    for item in sorted_items:
        item_cost = item.estimated_price * item.buy_list_item.target_quantity
        if running_total + item_cost <= budget_usd:
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


def _sellers_in_cart(result: CartResult) -> set[str]:
    """Return seller IDs from the items currently in a CartResult."""
    return {item.seller_id for item in result.items if item.seller_id}


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
        # card_id is now a required identifier (see _resolve_card_ids) — sourced from
        # ManaPool's own catalog via GET /products/singles, not derived/guessed.
        if item.card_id:
            entry["card_id"] = item.card_id
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


# Higher = smaller, more numerous groups per round (finer-grained per-item validation,
# since a group only gets merged in after checking out on its own — see
# _bisect_batch_removal), at the cost of more concurrent requests to ManaPool per round.
_DEFAULT_MAX_PARALLEL_TRIALS = 8


def _try_batch(
    items_to_remove: list[CartRequestItem],
    current: list[CartRequestItem],
    client: ManaPoolClient,
    run_kwargs: dict,
) -> CartResult | None:
    """Run one optimizer trial with `items_to_remove` pulled from `current`.

    Returns None (rather than raising) when the trial fails or removes everything, so
    callers can treat a failed trial as simply rejected — the same tolerance the
    sequential Phase 1/2 loops already have for a single bad trial.
    """
    remove_ids = {id(x) for x in items_to_remove}
    trial_set = [x for x in current if id(x) not in remove_ids]
    if not trial_set:
        return None
    try:
        return _run_single(trial_set, client, **run_kwargs)
    except ManaPoolAPIError as e:
        log.debug("Trial removing %d item(s) failed (%s)", len(items_to_remove), e)
        return None


def _chunk(items: list, n: int) -> list[list]:
    """Split items into up to n roughly-equal, non-empty chunks."""
    if not items:
        return []
    n = max(1, min(n, len(items)))
    size = -(-len(items) // n)  # ceil division
    return [items[i:i + size] for i in range(0, len(items), size)]


def _bisect_batch_removal(
    candidates: list[CartRequestItem],
    current: list[CartRequestItem],
    current_result: CartResult,
    client: ManaPoolClient,
    run_kwargs: dict,
    accept: Callable[[CartResult, CartResult], bool],
    max_parallel: int = _DEFAULT_MAX_PARALLEL_TRIALS,
) -> tuple[list[CartRequestItem], CartResult, list[CartRequestItem]]:
    """Try removing every item in `candidates` from `current`, using small parallel
    groups rather than one all-or-nothing sweep — so a removal only ever gets committed
    after it (or a small group containing it) has been checked on its own, not because
    it happened to ride along in a large batch whose *aggregate* looked fine.

    Deliberately does NOT try removing the whole candidate list in one call: a big
    batch passing net_value_usd doesn't mean every item in it individually deserved
    removal — a few genuinely-good items could ride along, their loss masked by
    everything else in the same batch actually being bad. Smaller groups make that
    much less likely without going all the way back to one-call-per-item.

    1. Split candidates into up to `max_parallel` groups and fire all of their removal
       trials at once (ThreadPoolExecutor — these are I/O-bound waits on ManaPool, not
       CPU work, so real Python threads parallelize them fine). Each group is tested
       standalone against the *same* current baseline.
    2. Groups whose standalone removal would be accepted are merged into one "validate
       the combined removal" call — a single trial removing all of them together, since
       two groups accepted independently can still interact (e.g. both leaning on the
       same seller's shipping) in ways a standalone probe can't see. This still only
       combines groups that already passed their own smaller-granularity check.
    3. If that combined validation holds up, commit it and recurse only on whatever's
       left over (re-chunked into smaller groups again next round). If it doesn't hold
       up, fall back to recursing into every group individually — same guarantee as
       before, just rarer to hit.
    4. A group of one candidate is the base case: test it directly, no further chunking.

    A failed API call for any trial counts as a rejected trial rather than raising.
    Returns (updated current, updated current_result, items actually removed).
    """
    if not candidates:
        return current, current_result, []

    if len(candidates) == 1:
        whole = _try_batch(candidates, current, client, run_kwargs)
        if whole is not None and accept(whole, current_result):
            remove_ids = {id(x) for x in candidates}
            return [x for x in current if id(x) not in remove_ids], whole, list(candidates)
        return current, current_result, []

    groups = _chunk(candidates, max_parallel)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = [pool.submit(_try_batch, g, current, client, run_kwargs) for g in groups]
        results = [f.result() for f in futures]

    accepted_mask = [res is not None and accept(res, current_result) for res in results]
    accepted_groups = [g for g, ok in zip(groups, accepted_mask) if ok]
    rejected_groups = [g for g, ok in zip(groups, accepted_mask) if not ok]

    if accepted_groups:
        merged_candidates = [x for g in accepted_groups for x in g]
        merged = _try_batch(merged_candidates, current, client, run_kwargs)
        if merged is not None and accept(merged, current_result):
            remove_ids = {id(x) for x in merged_candidates}
            new_current = [x for x in current if id(x) not in remove_ids]
            leftover = [x for g in rejected_groups for x in g]
            new_current, new_result, more_removed = _bisect_batch_removal(
                leftover, new_current, merged, client, run_kwargs, accept, max_parallel,
            )
            return new_current, new_result, merged_candidates + more_removed
        # Combined validation didn't hold up despite each group looking fine alone (an
        # interaction effect) — fall through to the full per-group recursion below,
        # since the baseline may shift as groups get applied and stale probe results
        # can no longer be trusted.
        removed_all: list[CartRequestItem] = []
        for g in groups:
            current, current_result, removed = _bisect_batch_removal(
                g, current, current_result, client, run_kwargs, accept, max_parallel,
            )
            removed_all.extend(removed)
        return current, current_result, removed_all

    # Nothing was accepted in the probe, so the baseline hasn't moved — a rejected
    # single-item group's result is still valid, no need to re-test it. Only groups
    # with more than one item need recursing into (to find a smaller sub-batch that
    # might still work within them).
    removed_all = []
    for g in groups:
        if len(g) == 1:
            continue
        current, current_result, removed = _bisect_batch_removal(
            g, current, current_result, client, run_kwargs, accept, max_parallel,
        )
        removed_all.extend(removed)
    return current, current_result, removed_all


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
    target_cart_usd: float | None = None,
    expansion_pool: list[CartRequestItem] | None = None,
    forced_card_names: frozenset[str] | None = None,
    max_parallel: int = _DEFAULT_MAX_PARALLEL_TRIALS,
) -> CartResult | None:
    """Find the cart configuration that maximizes net value.

    When max_cart_usd is set, the result's total (including shipping and fees)
    will not exceed that limit if at all possible.

    Args:
        target_cart_usd:   Initial build budget. Cards are pre-selected to fit within
                           this amount, leaving headroom for shipping and expansion.
                           Defaults to max_cart_usd × 0.80 when None.
        expansion_pool:    Additional CartRequestItems to try as free riders (Phase 3)
                           or new-seller candidates (Phase 4) after the main optimize.
        forced_card_names: Card names that must always be in the cart. Forced items
                           bypass the build-budget selection, are never removed in
                           Phase 2, and are re-added if their seller's package is
                           dropped in Phase 1 (the optimizer sources them elsewhere).
                           Their cost still counts toward max_cart_usd.
        max_parallel:      Phase 2's group size knob (see _bisect_batch_removal) —
                           higher means smaller, more numerous groups per round (each
                           validated standalone before merging) at the cost of more
                           concurrent calls per round. Set >= the candidate count to
                           make every group size 1 (no batching at all, still parallel).

    Iteration
    ---------
        1.  Build eligible items: estimated_price ≤ max_price × (1 + over_budget_pct%).
        2.  If target_cart_usd is set, greedily pre-select items within that budget,
            keeping the rest as an expansion pool for later phases. If target_cart_usd
            is None and max_cart_usd is set, defaults to max_cart_usd × 0.80.
        3.  Run optimizer → baseline result.
        4.  Phase 1: If cart total > max_cart_usd, remove worst-margin seller packages.
        5.  Phase 2a: batch-remove negative-margin items via _bisect_batch_removal —
            small parallel groups validated standalone before merging, not one blind
            whole-batch call. Phase 2b: same treatment for whatever's left
            (positive-margin but marginal items), as a separate pass. Neither is
            bounded by max_iterations.
        6.  Phase 3 (when expansion pool exists): add free-rider cards from sellers
            already in the cart — their shipping is already paid.
        7.  Phase 4 (when expansion pool exists): try the best-margin card from each
            new seller; check for free riders from that seller before accepting.

    Pass preselected to bypass build_request_items and _select_within_budget
    (e.g. when the caller has already done greedy budget packing for arbitrage).

    Total API calls: 1-2 (card_id resolution, batched at 100/call) + 1 (baseline)
                    + Phase 1 removals + Phase 2 (O(log n) typical, not one-per-item)
                    + Phase 3 free riders + Phase 4 (≤ max_iterations new-seller probes).
    """
    _run_expansion = target_cart_usd is not None or expansion_pool is not None
    forced_names: frozenset[str] = frozenset(forced_card_names or ())

    if preselected is not None:
        eligible = list(preselected)
        _extra_pool: list[CartRequestItem] = list(expansion_pool or [])
    else:
        all_eligible = build_request_items(match_results, over_budget_pct, scryfall=scryfall)

        # Forced items bypass the price filter — include them even when their listing
        # price exceeds max_price × (1 + over_budget_pct%). Build them separately
        # with a permissive threshold and merge any that aren't already present.
        if forced_names:
            forced_results = [r for r in match_results if r.buy_list_item.card_name in forced_names]
            if forced_results:
                already_forced = {x.buy_list_item.card_name for x in all_eligible if x.buy_list_item.card_name in forced_names}
                extra_forced = build_request_items(forced_results, over_budget_pct=99999.0, scryfall=scryfall)
                all_eligible = all_eligible + [x for x in extra_forced if x.buy_list_item.card_name not in already_forced]

        if not all_eligible:
            log.warning("No eligible items for cart optimization")
            return None

        forced_eligible = [x for x in all_eligible if x.buy_list_item.card_name in forced_names]
        optional_eligible = [x for x in all_eligible if x.buy_list_item.card_name not in forced_names]

        # When target_cart_usd is set the caller wants an explicit build budget with
        # headroom for shipping + expansion; otherwise fall back to the 20% reserve.
        build_budget = (
            target_cart_usd if target_cart_usd is not None
            else (max_cart_usd * 0.80 if max_cart_usd is not None else None)
        )

        if build_budget is not None:
            # Deduct forced items' estimated cost so they don't crowd out optional items.
            forced_cost = sum(
                x.estimated_price * x.buy_list_item.target_quantity for x in forced_eligible
            )
            optional_budget = max(0.0, build_budget - forced_cost)
            selected_optional = _select_within_budget(optional_eligible, optional_budget)
            eligible = selected_optional + forced_eligible
            if not eligible:
                log.warning(
                    "No items fit within the $%.2f build budget at estimated prices", build_budget
                )
                return None
            if target_cart_usd is not None or forced_names:
                selected_names = {x.buy_list_item.card_name for x in eligible}
                _overflow = [x for x in optional_eligible if x.buy_list_item.card_name not in selected_names]
                _run_expansion = True
            else:
                _overflow = []
        else:
            eligible = all_eligible
            _overflow = []

        _extra_pool = _overflow + list(expansion_pool or [])

    eligible = _resolve_card_ids(eligible, client)
    if not eligible:
        log.warning("No eligible items have a resolvable ManaPool card_id")
        return None
    _extra_pool = _resolve_card_ids(_extra_pool, client)

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

            # Forced items must remain in the cart even when their seller is removed.
            # Re-add them without a seller constraint; the optimizer sources elsewhere.
            if forced_names:
                displaced_forced = [x for x in worst_grp if x.buy_list_item.card_name in forced_names]
                if displaced_forced:
                    trial_set = trial_set + displaced_forced
                    log.info(
                        "Forced item(s) %s displaced from seller %r — will be re-sourced",
                        [x.buy_list_item.card_name for x in displaced_forced], worst_key,
                    )

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

            try:
                trial = _run_single(trial_set, client, **_run_kwargs)
            except ManaPoolAPIError as e:
                # A single failed trial (e.g. "no valid cart" for this combination)
                # shouldn't crash the whole run — treat it like a rejected trial and
                # keep going with the next-worst seller.
                log.warning("Trial removing seller %r failed (%s) — keeping it, locking it", worst_key, e)
                locked_sellers.add(worst_key)
                continue
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

    # Phase 2: Value optimization, batched — replaces the old one-at-a-time removal
    # loop with _bisect_batch_removal, run in two passes:
    #   2a. Negative-margin candidates only. Batching a *homogeneous* "these all look
    #       bad" set means the very first whole-batch trial usually succeeds outright
    #       (1 call resolves all of them), since removing genuinely unprofitable items
    #       together almost always helps net value.
    #   2b. Whatever's left (positive-margin but marginal items) gets the same
    #       batch/bisect treatment, just as its own pass — mixing profitable items into
    #       2a's batch would make that first whole-batch trial fail almost every time
    #       (removing a profitable item on top of the bad ones is rarely a net win),
    #       forcing unnecessary bisection. Keeping them separate lets 2a's common case
    #       stay fast while 2b still gets parallel probing instead of the old
    #       one-call-per-item loop.
    # Neither pass is bounded by max_iterations — batching removes the need for an
    # iteration cap here; max_iterations still bounds Phase 4's new-seller trials below.
    def _phase2_accept(trial: CartResult, baseline: CartResult) -> bool:
        return trial.net_value_usd >= baseline.net_value_usd

    def _run_phase2_pass(label: str, candidates: list[CartRequestItem]) -> None:
        nonlocal current, current_result, best
        if not candidates:
            return
        current, current_result, removed = _bisect_batch_removal(
            candidates, current, current_result, client, _run_kwargs, _phase2_accept, max_parallel,
        )
        if removed:
            log.info(
                "%s: removed %d item(s) — net value now $%+.2f",
                label, len(removed), current_result.net_value_usd,
            )
            if _is_better(current_result, best, max_cart_usd):
                best = current_result
        removed_ids = {id(x) for x in removed}
        kept = [x for x in candidates if id(x) not in removed_ids]
        if kept:
            log.info(
                "%s: kept %d item(s) — net value doesn't improve by removing them "
                "(shipping consolidation or already profitable)",
                label, len(kept),
            )
            locked.update(id(x) for x in kept)

    negative_candidates = [
        x for x in current
        if id(x) not in locked and x.buy_list_item.card_name not in forced_names
        and x.estimated_margin < 0
    ]
    _run_phase2_pass("Phase 2a", negative_candidates)

    marginal_candidates = [
        x for x in current if id(x) not in locked and x.buy_list_item.card_name not in forced_names
    ]
    _run_phase2_pass("Phase 2b", marginal_candidates)

    # Phase 3: Free-rider expansion — add cards from sellers already in the cart.
    # Their shipping is already paid so any positive-margin card from that seller
    # is pure upside. Try all candidates; each accepted item updates the cart.
    if _run_expansion and _extra_pool and best is not None and max_cart_usd is not None:
        existing_sellers = _sellers_in_cart(best)
        in_cart_names = {item.buy_list_item.card_name for item in best.items}
        free_riders = sorted(
            [x for x in _extra_pool
             if x.buy_list_item.card_name not in in_cart_names
             and x.seller_id in existing_sellers],
            key=lambda x: x.estimated_margin / x.estimated_price,
            reverse=True,
        )
        if free_riders:
            log.info(
                "Phase 3: checking %d free-rider candidate(s) from %d existing seller(s)",
                len(free_riders), len(existing_sellers),
            )
            best = try_add_items(
                best, free_riders, client,
                max_cart_usd=max_cart_usd,
                optimizer_model=optimizer_model,
                destination_country=destination_country,
                exclude_preorder=exclude_preorder,
            )

    # Phase 4: New-seller exploration — try adding the best-margin card from each
    # new seller, then check for free riders from that seller before deciding to
    # keep or reject the addition.
    if _run_expansion and _extra_pool and best is not None and max_cart_usd is not None:
        log.info("Phase 4: new-seller exploration (up to %d trial(s))", max_iterations)
        best = try_expand_with_new_sellers(
            best, _extra_pool, client,
            max_cart_usd=max_cart_usd,
            max_trials=max_iterations,
            optimizer_model=optimizer_model,
            destination_country=destination_country,
            exclude_preorder=exclude_preorder,
        )

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


def try_expand_with_new_sellers(
    current: CartResult,
    expansion_pool: list[CartRequestItem],
    client: ManaPoolClient,
    max_cart_usd: float | None = None,
    max_trials: int = 5,
    optimizer_model: str = "lowest_price",
    destination_country: str = "US",
    exclude_preorder: bool = False,
) -> CartResult:
    """Try adding best-margin cards from sellers not yet in the cart.

    For each candidate from a new seller (sorted by estimated_margin descending):
    1. Add the candidate and run the optimizer.
    2. Check for free riders from sellers newly introduced by this candidate,
       adding them via try_add_items.
    3. Accept the combined expansion if it improves net value within max_cart_usd.
    4. Reject and try the next candidate otherwise.

    Returns at least current. Makes at most max_trials optimizer calls for
    new-seller probes (plus inner try_add_items calls per accepted candidate).
    """
    _run_kwargs = dict(
        model=optimizer_model,
        destination=destination_country,
        exclude_universes_beyond=False,
        exclude_preorder=exclude_preorder,
    )

    best = current
    pool = list(expansion_pool)

    for _ in range(max_trials):
        current_sellers = _sellers_in_cart(best)
        in_cart_names = {item.buy_list_item.card_name for item in best.items}

        new_candidates = sorted(
            [x for x in pool
             if x.buy_list_item.card_name not in in_cart_names
             and (not x.seller_id or x.seller_id not in current_sellers)],
            key=lambda x: x.estimated_margin / x.estimated_price,
            reverse=True,
        )
        if not new_candidates:
            break

        candidate = new_candidates[0]
        trial_items = list(best.items) + [candidate]

        try:
            trial = _run_single(trial_items, client, **_run_kwargs)
        except ManaPool409Error:
            log.debug(
                "New-seller candidate %r not in optimizer index — skipping",
                candidate.buy_list_item.card_name,
            )
            pool = [x for x in pool if x.buy_list_item.card_name != candidate.buy_list_item.card_name]
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("New-seller candidate %r skipped: %s", candidate.buy_list_item.card_name, exc)
            pool = [x for x in pool if x.buy_list_item.card_name != candidate.buy_list_item.card_name]
            continue

        trial_sellers = {x.seller_id for x in trial_items if x.seller_id}
        new_sellers = trial_sellers - current_sellers
        if new_sellers:
            trial_in_cart = {x.buy_list_item.card_name for x in trial_items}
            new_seller_riders = sorted(
                [x for x in pool
                 if x.buy_list_item.card_name not in trial_in_cart
                 and x.seller_id in new_sellers],
                key=lambda x: x.estimated_margin,
                reverse=True,
            )
            if new_seller_riders:
                trial = try_add_items(
                    trial, new_seller_riders, client,
                    max_cart_usd=max_cart_usd,
                    optimizer_model=optimizer_model,
                    destination_country=destination_country,
                    exclude_preorder=exclude_preorder,
                )

        if _is_better(trial, best, max_cart_usd):
            log.info(
                "New-seller expansion: added %r (seller %r) — net $%.2f → $%.2f",
                candidate.buy_list_item.card_name,
                candidate.seller_id or "?",
                best.net_value_usd,
                trial.net_value_usd,
            )
            best = trial
        else:
            log.info(
                "New-seller expansion: rejected %r — net would be $%.2f vs current $%.2f",
                candidate.buy_list_item.card_name,
                trial.net_value_usd,
                best.net_value_usd,
            )
            pool = [x for x in pool if x.buy_list_item.card_name != candidate.buy_list_item.card_name]

    return best
