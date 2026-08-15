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
        within that estimated dollar cap (sorted by relative margin rate × qty).
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
    target_cart_usd: float | None = None,
    expansion_pool: list[CartRequestItem] | None = None,
    forced_card_names: frozenset[str] | None = None,
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

    Iteration
    ---------
        1.  Build eligible items: estimated_price ≤ max_price × (1 + over_budget_pct%).
        2.  If target_cart_usd is set, greedily pre-select items within that budget,
            keeping the rest as an expansion pool for later phases. If target_cart_usd
            is None and max_cart_usd is set, defaults to max_cart_usd × 0.80.
        3.  Run optimizer → baseline result.
        4.  Phase 1: If cart total > max_cart_usd, remove worst-margin seller packages.
        5.  Phase 2: Remove negative-margin items up to max_iterations times.
        6.  Phase 3 (when expansion pool exists): add free-rider cards from sellers
            already in the cart — their shipping is already paid.
        7.  Phase 4 (when expansion pool exists): try the best-margin card from each
            new seller; check for free riders from that seller before accepting.

    Pass preselected to bypass build_request_items and _select_within_budget
    (e.g. when the caller has already done greedy budget packing for arbitrage).

    Total API calls: 1 (baseline) + Phase 1 removals + Phase 2 (≤ max_iterations)
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
    # Forced items are never removed regardless of their margin.
    for iteration in range(max_iterations):
        candidates = [x for x in current if id(x) not in locked and x.buy_list_item.card_name not in forced_names]
        if not candidates:
            break

        worst = min(candidates, key=lambda x: x.estimated_margin / x.estimated_price)
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
