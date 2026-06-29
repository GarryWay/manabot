from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from manabot import __version__

log = logging.getLogger(__name__)


@click.group()
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Manabot — MTG price monitoring bot for ManaPool."""
    ctx.ensure_object(dict)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None, help="Path to config.yaml.")
@click.option("--buylist", "buylist_path_override", type=click.Path(path_type=Path), default=None, help="Path to buy list CSV.")
@click.option("--dry-run", "-n", is_flag=True, help="Skip Discord send; print payload instead.")
@click.option("--notify-always", is_flag=True, help="Send Discord alert even if no good buys found.")
@click.option("--no-html", is_flag=True, help="Skip HTML report generation.")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def run(
    ctx: click.Context,
    config_path: Path | None,
    buylist_path_override: Path | None,
    dry_run: bool,
    notify_always: bool,
    no_html: bool,
    verbose: bool,
) -> None:
    """Fetch prices, match against buy list, and report results."""
    _configure_logging(verbose)
    from manabot.config import load_config
    from manabot.buylist import load_buylist
    from manabot.db import open_db, insert_listings, record_fetch_run
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall import ScryfallClient
    from manabot.matcher import match
    from manabot.analyzer import analyze, summarize
    from manabot.reporter import terminal, html as html_reporter, csv_report, discord

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    buylist_path = buylist_path_override or config.buylist_path
    try:
        buy_list = load_buylist(buylist_path)
    except (FileNotFoundError, Exception) as e:
        click.echo(f"Buy list error: {e}", err=True)
        sys.exit(1)

    log.info("Loaded %d buy list items from %s", len(buy_list), buylist_path)

    # Scryfall client is used only for per-listing in-universe filtering during match().
    # We intentionally do NOT call enrich_buylist() here: items without a scryfall_id
    # use name-based matching so that all printings remain candidates, and each
    # individual listing is then checked via is_in_universe(listing.scryfall_id).
    # Use `validate-buylist --suggest-ids` to populate scryfall_ids when you want
    # to pin a specific printing.
    scryfall_client = ScryfallClient()

    client = ManaPoolClient(
        email=config.manapool_email,
        token=config.manapool_token,
        use_bulk_export=config.use_bulk_export,
    )

    started_at = datetime.now(timezone.utc)
    try:
        listings = client.get_singles_prices()
    except Exception as e:
        click.echo(f"ManaPool API error: {e}", err=True)
        sys.exit(1)

    log.info("Fetched %d listings from ManaPool", len(listings))

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)
        log.debug("Inserted %d snapshots into DB", len(listings))

        results = match(buy_list, listings, scryfall_client=scryfall_client)
        results = analyze(results, conn, config.trend_window_days, config.trend_threshold_pct)

        summary = summarize(results)
        completed_at = datetime.now(timezone.utc)
        record_fetch_run(conn, started_at, completed_at, len(listings), summary["good_buy_count"])

    terminal.render(results)

    if not no_html:
        html_path = html_reporter.write(results, config.reports_dir, completed_at, summary)
        csv_path = csv_report.write(results, config.reports_dir, completed_at)
        log.info("Reports written: %s, %s", html_path, csv_path)

    if summary["good_buy_count"] > 0 or notify_always:
        discord.send(results, config.discord_webhook_url, summary, completed_at, dry_run=dry_run)
    elif dry_run:
        click.echo("[dry-run] No good buys found — Discord notification would be skipped.")

    click.echo(
        f"\nDone. {summary['good_buy_count']}/{summary['total_checked']} good buys, "
        f"{summary['unresolved_count']} unresolved."
    )


@cli.command()
@click.option("--card", required=True, help="Card name or Scryfall ID to look up.")
@click.option("--days", default=30, show_default=True, help="Number of days of history to show.")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def history(ctx: click.Context, card: str, days: int, config_path: Path | None) -> None:
    """Show price history for a card."""
    import re
    from manabot.config import load_config
    from manabot.db import init_db, get_price_history

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    conn = init_db(config.db_path)
    try:
        # Try as Scryfall ID (UUID format) or fall back to name lookup in DB
        is_uuid = bool(re.match(r"^[0-9a-f-]{36}$", card, re.I))
        if is_uuid:
            scryfall_id = card
        else:
            row = conn.execute(
                "SELECT scryfall_id FROM price_snapshots WHERE LOWER(card_name) = LOWER(?) LIMIT 1",
                (card,),
            ).fetchone()
            if not row:
                click.echo(f"No history found for {card!r}.", err=True)
                sys.exit(1)
            scryfall_id = row["scryfall_id"]

        history_data = get_price_history(conn, scryfall_id, days=days)
        if not history_data:
            click.echo(f"No price history found for {card!r} in the last {days} days.")
            return

        click.echo(f"Price history for {card!r} (last {days} days):")
        for date, price in history_data:
            click.echo(f"  {date.strftime('%Y-%m-%d')}  ${price:.2f}")
    finally:
        conn.close()


@cli.command("validate-buylist")
@click.option("--buylist", "buylist_path_override", type=click.Path(path_type=Path), default=None)
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def validate_buylist(ctx: click.Context, buylist_path_override: Path | None, config_path: Path | None) -> None:
    """Validate the buy list CSV and report any issues."""
    from manabot.config import load_config
    from manabot.buylist import load_buylist, BuyListError

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    buylist_path = buylist_path_override or config.buylist_path
    try:
        items = load_buylist(buylist_path)
        click.echo(f"Buy list OK — {len(items)} items loaded from {buylist_path}")

        unresolved_sets = [i for i in items if i.scryfall_id is None]
        if unresolved_sets:
            click.echo(f"\n{len(unresolved_sets)} item(s) have no scryfall_id (will use name matching):")
            for item in unresolved_sets:
                click.echo(f"  - {item.card_name}")

        needs_scryfall = [i for i in items if i.in_universe_only]
        if needs_scryfall:
            click.echo(f"\n{len(needs_scryfall)} item(s) require Scryfall (in_universe_only=true):")
            for item in needs_scryfall:
                click.echo(f"  - {item.card_name}")

    except BuyListError as e:
        click.echo(f"Buy list validation failed:\n{e}", err=True)
        sys.exit(1)
    except FileNotFoundError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--buylist", "buylist_path_override", type=click.Path(path_type=Path), default=None)
@click.option("--margin-pct", type=float, default=None,
              help="Require cards to be at least X% below max_price_usd (default 0; "
                   "negative values allow going over budget).")
@click.option("--max-cart-usd", type=float, default=None,
              help="Hard spending cap per run — buy only the best items that fit within this total.")
@click.option("--target-cart-usd", default=None, type=float,
              help="Target spend before shipping/fees (leaves room for free-rider expansion). "
                   "Defaults to 80% of --max-cart-usd.")
@click.option("--max-iterations", type=int, default=None,
              help="Max optimizer removal trials (default 5).")
@click.option("--destination", default=None, help="Shipping destination: US or CA (default US).")
@click.option("--dry-run", "-n", is_flag=True,
              help="Print optimizer payload; do not call the optimizer API.")
@click.option("--submit-cart", is_flag=True,
              help="After optimizing, add the result to your ManaPool cart for review.")
@click.option("--arb-riders", is_flag=True, default=False,
              help="After optimizing, try adding arbitrage free-riders from sellers already in the cart.")
@click.option("--force-card", "force_cards", multiple=True, metavar="NAME",
              help="Force a card from the buy list into the cart regardless of margin (repeatable). "
                   "The card's cost counts toward budget but it cannot be removed by the optimizer.")
@click.option("--exclude-preorder/--no-exclude-preorder", default=True, show_default=True,
              help="Exclude pre-order listings from optimizer results.")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def optimize(
    ctx: click.Context,
    config_path: Path | None,
    buylist_path_override: Path | None,
    margin_pct: float | None,
    max_cart_usd: float | None,
    target_cart_usd: float | None,
    max_iterations: int | None,
    destination: str | None,
    dry_run: bool,
    submit_cart: bool,
    arb_riders: bool,
    force_cards: tuple[str, ...],
    exclude_preorder: bool,
    verbose: bool,
) -> None:
    """Find the highest-value cart using the ManaPool optimizer.

    Fetches current prices, matches your buy list, then iterates through cart
    configurations to maximize: sum(max_price_usd * qty) - total_cart_cost.
    Use --max-cart-usd to cap total spend (e.g. for weekly budget purchases).
    """
    _configure_logging(verbose)
    import json as _json
    from manabot.config import load_config
    from manabot.buylist import load_buylist
    from manabot.db import open_db, insert_listings
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall import ScryfallClient
    from manabot.api.scryfall_bulk import ScryfallBulk
    from manabot.matcher import match
    from manabot.analyzer import analyze
    from manabot.models import MatchStatus
    import manabot.optimizer as opt

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    buylist_path = buylist_path_override or config.buylist_path
    try:
        buy_list = load_buylist(buylist_path)
    except Exception as e:
        click.echo(f"Buy list error: {e}", err=True)
        sys.exit(1)

    # margin_pct is the user-facing parameter; internally the optimizer uses over_budget_pct
    # (negative = must be below max price). Invert sign: margin=10 → over_budget=-10.
    effective_over_budget = (-margin_pct) if margin_pct is not None else config.optimizer_over_budget_pct
    effective_max_cart = max_cart_usd if max_cart_usd is not None else config.optimizer_max_cart_usd
    effective_target = target_cart_usd if target_cart_usd is not None else config.optimizer_target_cart_usd
    effective_max_iter = max_iterations if max_iterations is not None else config.optimizer_max_iterations
    effective_dest = destination or config.optimizer_destination

    scryfall_client = ScryfallClient()
    scryfall_bulk = ScryfallBulk()
    client = ManaPoolClient(
        email=config.manapool_email,
        token=config.manapool_token,
        use_bulk_export=config.use_bulk_export,
    )

    try:
        listings = client.get_singles_prices()
    except Exception as e:
        click.echo(f"ManaPool API error: {e}", err=True)
        sys.exit(1)

    log.info("Fetched %d listings from ManaPool", len(listings))

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)
        results = match(buy_list, listings, scryfall_client=scryfall_client)
        results = analyze(results, conn, config.trend_window_days, config.trend_threshold_pct)

    matched_count = sum(1 for r in results if r.status == MatchStatus.MATCHED)
    click.echo(f"Matched {matched_count}/{len(buy_list)} buy list items.")

    eligible = opt.build_request_items(
        results, effective_over_budget,
        scryfall=scryfall_bulk if scryfall_bulk.available else None,
    )
    if not eligible:
        margin_display = -effective_over_budget
        click.echo(
            "\nNo eligible items to optimize. "
            f"Try lowering --margin-pct (currently {margin_display:.0f}%)."
        )
        sys.exit(0)

    margin_display = -effective_over_budget
    budget_note = f", cap ${effective_max_cart:.2f}" if effective_max_cart is not None else ""
    click.echo(
        f"\n{len(eligible)} item(s) eligible"
        f" (margin ≥{margin_display:.0f}% below max price{budget_note})."
    )

    if dry_run:
        payload = opt._build_optimizer_payload(eligible)
        click.echo("\n[dry-run] Would POST to /buyer/optimizer:")
        click.echo(_json.dumps(
            {"cart": payload, "model": "lowest_price", "destination_country": effective_dest},
            indent=2,
        ))
        return

    click.echo(f"\nRunning optimizer (up to {effective_max_iter} iteration(s))...")

    arb_expansion: list | None = None
    if arb_riders:
        import manabot.arbitrage as arb_mod
        from manabot.api.scryfall_bulk import ScryfallBulk as _SBulk
        _sb = _SBulk()
        arb_cands = arb_mod.find_candidates(
            listings,
            scryfall=_sb if _sb.available else None,
            min_discount_pct=10.0,
            min_quantity=20,
            min_market_price_usd=config.arbitrage_min_market_price_usd,
        )
        if arb_cands:
            arb_results = arb_mod.candidates_to_match_results(arb_cands)
            arb_expansion = opt.build_request_items(arb_results, over_budget_pct=0.0)
            click.echo(
                f"\nArbitrage expansion pool: {len(arb_expansion)} candidate(s) for free-rider/new-seller fill."
            )

    effective_forced = frozenset(force_cards) if force_cards else None
    if effective_forced:
        click.echo(f"\nForced cards: {', '.join(sorted(effective_forced))}")

    try:
        cart = opt.find_best_cart(
            results,
            client,
            over_budget_pct=effective_over_budget,
            target_cart_usd=effective_target,
            max_cart_usd=effective_max_cart,
            max_iterations=effective_max_iter,
            destination_country=effective_dest,
            scryfall=scryfall_bulk if scryfall_bulk.available else None,
            exclude_preorder=exclude_preorder,
            expansion_pool=arb_expansion,
            forced_card_names=effective_forced,
        )
    except Exception as e:
        click.echo(f"Optimizer error: {e}", err=True)
        sys.exit(1)

    if cart is None:
        click.echo("Optimizer returned no result.")
        sys.exit(0)

    buylist_names = {r.buy_list_item.card_name for r in results}
    n_buylist_items = sum(1 for x in cart.items if x.buy_list_item.card_name in buylist_names)
    n_arb_added = len(cart.items) - n_buylist_items
    buylist_note = f"{n_buylist_items} buylist"
    if n_arb_added:
        buylist_note += f" + {n_arb_added} arbitrage free-rider(s)"
    click.echo(f"\nOptimized cart — {len(cart.items)} item(s) ({buylist_note}):")
    for x in cart.items:
        qty = x.buy_list_item.target_quantity
        line_est = x.estimated_price * qty
        sign = "+" if x.estimated_margin >= 0 else "-"
        qty_str = f" ×{qty}" if qty > 1 else ""
        click.echo(
            f"  {x.buy_list_item.card_name:<30s}  [{x.set_code}]"
            f"  est. ${x.estimated_price:.2f}{qty_str} (${line_est:.2f})"
            f"  (margin {sign}${abs(x.estimated_margin):.2f}/ea)"
        )
    click.echo(f"  {'— est. prices are pre-fetch; subtotal is optimizer actual —':^72s}")

    click.echo(f"\n  Subtotal : ${cart.subtotal_usd:.2f}")
    click.echo(f"  Shipping : ${cart.shipping_usd:.2f}")
    click.echo(f"  Fees     : ${cart.fees_usd:.2f}")
    click.echo(f"  Total    : ${cart.total_usd:.2f}")
    click.echo(f"  Budget   : ${cart.value_budget_usd:.2f}")
    click.echo(f"  Net value: ${cart.net_value_usd:+.2f}")

    if effective_max_cart is not None and cart.total_usd > effective_max_cart:
        click.echo(f"\n  Warning: cart total ${cart.total_usd:.2f} exceeds spending cap ${effective_max_cart:.2f}.")
    elif cart.is_profitable:
        click.echo("\n  Cart is profitable relative to your buy list values.")
    else:
        click.echo("\n  Warning: cart total exceeds buy list value budget.")

    if submit_cart:
        _submit_pending_order(cart, client, config, effective_max_cart)
    else:
        click.echo("\nRun with --submit-cart to create a pending order on ManaPool for review.")


def _submit_pending_order(cart, client, config, max_cart_usd):
    """Validate and submit a CartResult as a pending order on ManaPool."""
    if not cart.raw_cart:
        click.echo("\nNo inventory IDs in optimizer result — cannot create order.", err=True)
        sys.exit(1)
    if max_cart_usd is not None and cart.total_usd > max_cart_usd:
        click.echo(
            f"\nRefusing to submit: cart total ${cart.total_usd:.2f} exceeds cap ${max_cart_usd:.2f}.",
            err=True,
        )
        sys.exit(1)
    if cart.net_value_usd < 0:
        click.echo(
            f"\nRefusing to submit: cart net value is ${cart.net_value_usd:.2f} (negative).",
            err=True,
        )
        sys.exit(1)
    if config.shipping_address is None:
        click.echo(
            "\nNo shipping_address configured. "
            "Add it to config.yaml under optimizer.shipping_address "
            "(fields: name, line1, city, state, postal_code, country).",
            err=True,
        )
        sys.exit(1)
    click.echo(f"\nCreating pending order ({len(cart.raw_cart)} line item(s))...")
    try:
        order = client.create_pending_order(cart.raw_cart, shipping_address=config.shipping_address)
    except Exception as e:
        click.echo(f"Order creation failed: {e}", err=True)
        sys.exit(1)
    totals = order.get("totals", {})
    tax = totals.get("tax_cents", 0) / 100.0
    total_with_tax = totals.get("total_cents", 0) / 100.0
    net_after_tax = cart.value_budget_usd - total_with_tax
    click.echo(f"\n  Pending order ID : {order['id']}")
    click.echo(f"  Status           : {order.get('status', '?')}")
    click.echo(f"  Tax              : ${tax:.2f}")
    click.echo(f"  Total (with tax) : ${total_with_tax:.2f}")
    click.echo(f"  Net value (after tax): ${net_after_tax:+.2f}")
    click.echo("\nRun: manabot order-info " + order['id'])
    log.debug("Pending order response: %s", order)


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--min-discount-pct", type=float, default=10.0, show_default=True,
              help="Minimum % below market price to qualify as a candidate.")
@click.option("--min-quantity", type=int, default=20, show_default=True,
              help="Minimum available quantity (liquidity proxy).")
@click.option("--min-market-price", type=float, default=None,
              help="Minimum NM floor price (USD) to exclude bulk commons. Overrides config.")
@click.option("--max-cart-usd", type=float, default=None,
              help="Hard spending cap on the resulting cart.")
@click.option("--target-cart-usd", type=float, default=None,
              help="Initial build target before free-rider expansion. Defaults to 80%% of --max-cart-usd.")
@click.option("--max-iterations", type=int, default=None,
              help="Max optimizer removal trials (default 5).")
@click.option("--destination", default=None, help="Shipping destination: US or CA.")
@click.option("--submit-cart", is_flag=True,
              help="Create a pending order on ManaPool after optimizing.")
@click.option("--dry-run", "-n", is_flag=True,
              help="Show candidates and optimizer payload; do not call the optimizer.")
@click.option("--min-liquidity", type=float, default=None,
              help="Minimum sales per 30 days to include a card (0 = no filter). Overrides config.")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def arbitrage(
    ctx: click.Context,
    config_path: Path | None,
    min_discount_pct: float,
    min_quantity: int,
    min_market_price: float,
    max_cart_usd: float | None,
    target_cart_usd: float | None,
    max_iterations: int | None,
    destination: str | None,
    submit_cart: bool,
    dry_run: bool,
    min_liquidity: float | None,
    verbose: bool,
) -> None:
    """Find ManaPool listings trading below market value and optimize a resale cart.

    Scans all live listings, identifies cards priced below their market reference
    (price_market) by at least --min-discount-pct, then runs the optimizer to
    find the highest net-value cart after shipping and fees.
    """
    import json as _json
    _configure_logging(verbose)
    from manabot.config import load_config
    from manabot.db import open_db, insert_listings
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall_bulk import ScryfallBulk
    from manabot.models import MatchStatus
    import manabot.optimizer as opt
    import manabot.arbitrage as arb

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    effective_max_iter = max_iterations if max_iterations is not None else config.optimizer_max_iterations
    effective_dest = destination or config.optimizer_destination
    effective_max_cart = max_cart_usd if max_cart_usd is not None else config.optimizer_max_cart_usd
    effective_target = target_cart_usd if target_cart_usd is not None else config.optimizer_target_cart_usd
    effective_min_market = min_market_price if min_market_price is not None else config.arbitrage_min_market_price_usd
    effective_min_liquidity = min_liquidity if min_liquidity is not None else config.arbitrage_min_liquidity_sales

    client = ManaPoolClient(
        email=config.manapool_email,
        token=config.manapool_token,
        use_bulk_export=config.use_bulk_export,
    )

    try:
        listings = client.get_singles_prices()
    except Exception as e:
        click.echo(f"ManaPool API error: {e}", err=True)
        sys.exit(1)

    log.info("Fetched %d listings from ManaPool", len(listings))

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)

    scryfall = ScryfallBulk()
    if not scryfall.available:
        click.echo(
            "Warning: Scryfall bulk data not found — non-sanctioned cards will not be filtered.\n"
            "Run scripts/populate_buylist.py to download it.",
            err=True,
        )
        scryfall = None  # type: ignore[assignment]

    # Load catalog for liquidity scoring (optional — warn and continue if unavailable)
    catalog_records: list[dict] | None = None
    if effective_min_liquidity > 0.0 or True:  # always try; liquidity data enriches output
        try:
            from manabot.api.manapool_catalog import load_catalog
            catalog_records = load_catalog(config.catalog_cache_path)
            log.info("Catalog loaded: %d records for liquidity scoring", len(catalog_records))
        except Exception as e:
            log.warning("Could not load catalog for liquidity scoring: %s", e)

    candidates = arb.find_candidates(
        listings,
        scryfall=scryfall,
        min_discount_pct=min_discount_pct,
        min_quantity=min_quantity,
        min_market_price_usd=effective_min_market,
        catalog_records=catalog_records,
        min_liquidity_sales=effective_min_liquidity,
        liquidity_lookback_days=config.arbitrage_liquidity_lookback_days,
    )

    if not candidates:
        click.echo(
            f"No arbitrage candidates found "
            f"(min discount {min_discount_pct:.0f}%, min qty {min_quantity}, "
            f"min market ${effective_min_market:.2f})."
        )
        sys.exit(0)

    budget_note = f", cap ${effective_max_cart:.2f}" if effective_max_cart is not None else ""
    click.echo(
        f"\n{len(candidates)} candidate(s) found "
        f"(≥{min_discount_pct:.0f}% below market, ≥{min_quantity} available, "
        f"market ≥${effective_min_market:.2f}{budget_note}):\n"
    )
    click.echo(f"  {'Card':<40s} {'Set':>4s}  {'List':>7s}  {'NM Mkt':>7s}  {'Discount':>8s}  {'Avail':>5s}")
    click.echo(f"  {'-'*40} {'-'*4}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*5}")
    for c in candidates[:50]:  # cap display at 50 rows
        click.echo(
            f"  {c.listing.card_name:<40s} {c.listing.set_code:>4s}"
            f"  ${c.listing.price_usd:>6.2f}  ${c.market_price_usd:>6.2f}"
            f"  {c.discount_pct:>7.1f}%  {c.listing.quantity_available:>5d}"
        )
        if c.listing.url:
            click.echo(f"    {c.listing.url}")
    if len(candidates) > 50:
        click.echo(f"  ... and {len(candidates) - 50} more")

    # Pre-filter: skip any card where a single copy already exceeds the cart cap.
    if effective_max_cart is not None:
        affordable = [c for c in candidates if c.listing.price_usd <= effective_max_cart]
        skipped_over_cap = len(candidates) - len(affordable)
        if skipped_over_cap:
            click.echo(f"\nExcluded {skipped_over_cap} candidate(s) priced above cart cap (${effective_max_cart:.2f}).")
        if not affordable:
            click.echo("No candidates remain after excluding cards above the cart cap.")
            sys.exit(0)
    else:
        affordable = list(candidates)

    # Greedy selection: one copy of each candidate, highest-discount first.
    # Reserve ~35% for shipping + fees. Cheap arbitrage cards often come from many
    # different sellers; shipping per seller ($1-2) adds up quickly and can exceed
    # 30-40% of card cost when buying 30+ items from 20+ sellers.
    budget_remaining = (effective_max_cart * 0.65) if effective_max_cart is not None else float("inf")

    selected_candidates: list[arb.ArbitrageCandidate] = []
    for c in affordable:  # already sorted by discount_pct descending
        price = c.listing.price_usd
        if price > budget_remaining:
            continue  # can't afford this one; keep looking at cheaper candidates
        selected_candidates.append(c)
        budget_remaining -= price
        if budget_remaining < 0.50:
            break

    if not selected_candidates:
        click.echo("\nNo candidates fit within the estimated budget.")
        sys.exit(0)

    match_results = arb.candidates_to_match_results(selected_candidates)
    prebuilt = opt.build_request_items(match_results, over_budget_pct=0.0)

    if not prebuilt:
        click.echo("\nNo eligible items after optimizer filtering.")
        sys.exit(0)

    total_estimated = sum(
        x.estimated_price * x.buy_list_item.target_quantity for x in prebuilt
    )

    if dry_run:
        payload = opt._build_optimizer_payload(prebuilt)
        click.echo(
            f"\n[dry-run] Would POST {len(payload)} item(s) (est. ${total_estimated:.2f}) "
            f"to /buyer/optimizer:"
        )
        click.echo(_json.dumps(
            {"cart": payload, "model": "lowest_price", "destination_country": effective_dest},
            indent=2,
        ))
        return

    click.echo(
        f"\nRunning optimizer on {len(prebuilt)} candidate(s) "
        f"(est. ${total_estimated:.2f}, up to {effective_max_iter} iteration(s))..."
    )

    try:
        cart = opt.find_best_cart(
            match_results,
            client,
            over_budget_pct=0.0,
            target_cart_usd=effective_target,
            max_cart_usd=effective_max_cart,
            max_iterations=effective_max_iter,
            destination_country=effective_dest,
            preselected=prebuilt,
        )
    except Exception as e:
        click.echo(f"Optimizer error: {e}", err=True)
        sys.exit(1)

    if cart is None:
        click.echo("Optimizer returned no result.")
        sys.exit(0)

    # Phase 3: Free-rider fill — try adding overflow candidates from sellers already in
    # the cart. Their shipping is already paid, so any positive-margin card from that
    # seller improves net value at zero extra cost.
    selected_set = {id(c) for c in selected_candidates}
    overflow = [c for c in affordable if id(c) not in selected_set]
    if overflow:
        cart_seller_ids = {x.seller_id for x in cart.items if x.seller_id}
        if cart_seller_ids:
            overflow_results = arb.candidates_to_match_results(overflow)
            overflow_items = opt.build_request_items(overflow_results, over_budget_pct=0.0)
            free_riders = [x for x in overflow_items if x.seller_id in cart_seller_ids]
            if free_riders:
                free_riders = free_riders[:effective_max_iter]  # cap API calls
                click.echo(
                    f"\nFree-rider fill: checking {len(free_riders)} candidate(s) "
                    f"from {len(cart_seller_ids)} existing seller(s)..."
                )
                cart = opt.try_add_items(
                    cart,
                    free_riders,
                    client,
                    max_cart_usd=effective_max_cart,
                    destination_country=effective_dest,
                )

    click.echo(f"\nOptimized arbitrage cart — {len(cart.items)} item(s):")
    for x in cart.items:
        qty = x.buy_list_item.target_quantity
        line_est = x.estimated_price * qty
        discount = x.estimated_margin / x.buy_list_item.max_price_usd * 100 if x.buy_list_item.max_price_usd else 0
        qty_str = f" ×{qty}" if qty > 1 else ""
        click.echo(
            f"  {x.buy_list_item.card_name:<40s}"
            f"  est. ${x.estimated_price:.2f}{qty_str} (${line_est:.2f})"
            f"  market ${x.buy_list_item.max_price_usd:.2f}"
            f"  ({discount:.1f}% below)"
        )
    click.echo(f"  {'— est. prices are pre-fetch; subtotal is optimizer actual —':^72s}")

    click.echo(f"\n  Subtotal : ${cart.subtotal_usd:.2f}")
    click.echo(f"  Shipping : ${cart.shipping_usd:.2f}")
    click.echo(f"  Fees     : ${cart.fees_usd:.2f}")
    click.echo(f"  Total    : ${cart.total_usd:.2f}")
    click.echo(f"  Market value of cart : ${cart.value_budget_usd:.2f}")
    click.echo(f"  Net value (resale)   : ${cart.net_value_usd:+.2f}")

    if cart.net_value_usd <= 0:
        click.echo("\n  Warning: cart has no positive net value after costs.")
    else:
        click.echo(f"\n  Estimated profit margin: {cart.net_value_usd / cart.total_usd * 100:.1f}%")

    # Per-seller breakdown — shows where the gross margin is concentrated and what the
    # average shipping cost per seller package looks like.
    seller_groups = opt._group_by_seller(cart.items)
    named_groups = [(k, g) for k, g in seller_groups if not k.startswith("__solo_")]
    if named_groups:
        n_sellers = len(named_groups)
        avg_ship = cart.shipping_usd / n_sellers if n_sellers else 0.0
        click.echo(
            f"\nPer-seller breakdown  ({n_sellers} seller(s), "
            f"avg ${avg_ship:.2f} shipping/package):"
        )
        click.echo(
            f"  {'Seller':<24s}  {'Items':>5s}  {'Cost':>7s}  {'Market':>7s}  {'Gross':>7s}"
        )
        click.echo(f"  {'-'*24}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*7}")
        for key, items in seller_groups:
            if key.startswith("__solo_"):
                continue
            cost = sum(x.estimated_price for x in items)
            market = sum(x.buy_list_item.max_price_usd for x in items)
            gross = market - cost
            flag = "  ← low margin" if gross < avg_ship else ""
            click.echo(
                f"  {key[:24]:<24s}  {len(items):>5d}  ${cost:>6.2f}  ${market:>6.2f}"
                f"  ${gross:>+6.2f}{flag}"
            )

    if submit_cart:
        _submit_pending_order(cart, client, config, effective_max_cart)
    else:
        click.echo("\nRun with --submit-cart to create a pending order on ManaPool for review.")


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--guild", "guild_id", type=int, default=None,
              help="Sync slash commands to this guild ID instantly (useful during development).")
@click.pass_context
def bot(ctx: click.Context, config_path: Path | None, guild_id: int | None) -> None:
    """Start the Discord slash-command bot.

    Requires discord.py:  pip install ".[bot]"
    Configure DISCORD_BOT_TOKEN in .env or discord.bot_token in config.yaml.
    Use --guild <ID> during development to sync commands instantly to a single guild.
    """
    from manabot.config import load_config

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    if guild_id is not None:
        config.discord_guild_id = guild_id

    try:
        from manabot.discord_bot import run_bot
    except ImportError:
        click.echo(
            "discord.py is not installed. Run:  pip install \".[bot]\"",
            err=True,
        )
        sys.exit(1)

    run_bot(config)


@cli.command()
@click.pass_context
def scheduled(ctx: click.Context) -> None:
    """Run on a schedule (requires APScheduler — not yet configured)."""
    from manabot.scheduler import schedule_run
    try:
        schedule_run("", None)
    except NotImplementedError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@cli.command("order-info")
@click.argument("order_id")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--json", "dump_json", is_flag=True, default=False,
              help="Print raw JSON response.")
def order_info(order_id: str, config_path: Path | None, dump_json: bool) -> None:
    """Fetch a pending order by ID and display its details.

    ORDER_ID is the UUID returned by --submit-cart (e.g. 14f0fe8e-8392-...).
    """
    import json as _json
    from manabot.config import load_config
    from manabot.api.manapool import ManaPoolClient

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    client = ManaPoolClient(
        email=config.manapool_email,
        token=config.manapool_token,
    )

    try:
        order = client.get_pending_order(order_id)
    except Exception as e:
        click.echo(f"Error fetching order: {e}", err=True)
        sys.exit(1)

    if dump_json:
        click.echo(_json.dumps(order, indent=2))
        return

    totals = order.get("totals", {})
    subtotal = totals.get("subtotal_cents", 0) / 100.0
    shipping = totals.get("shipping_cents", 0) / 100.0
    tax = totals.get("tax_cents", 0) / 100.0
    total = totals.get("total_cents", 0) / 100.0

    click.echo(f"\nOrder ID : {order.get('id', order_id)}")
    click.echo(f"Status   : {order.get('status', '?')}")

    line_items = order.get("line_items") or order.get("items") or []
    if line_items:
        inv_ids = [item["inventory_id"] for item in line_items if "inventory_id" in item]
        qty_map = {item["inventory_id"]: item.get("quantity_selected", 1)
                   for item in line_items if "inventory_id" in item}
        details_map: dict[str, dict] = {}
        if inv_ids:
            try:
                details = client.get_inventory_details(inv_ids)
                details_map = {d["id"]: d for d in details if "id" in d}
            except Exception as e:
                click.echo(f"  (Could not fetch card details: {e})", err=True)

        click.echo(f"\nLine items ({len(line_items)}):")
        for inv_id in inv_ids:
            qty = qty_map.get(inv_id, 1)
            detail = details_map.get(inv_id)
            if detail:
                prod = detail.get("product", {}).get("single", {})
                name = prod.get("name", "?")
                set_code = prod.get("set", "?").upper()
                cond = prod.get("condition_id", "?")
                finish = prod.get("finish_id", "?")
                price = detail.get("price_cents", 0) / 100.0
                click.echo(f"  {qty}x {name:<40s} [{set_code}, {cond}, {finish}]  ${price:.2f}")
            else:
                click.echo(f"  {qty}x {inv_id}")

    fees = totals.get("buyer_fee_cents", totals.get("fees_cents", totals.get("fee_cents", 0))) / 100.0
    click.echo(f"\n  Subtotal : ${subtotal:.2f}")
    click.echo(f"  Shipping : ${shipping:.2f}")
    click.echo(f"  Fees     : ${fees:.2f}")
    click.echo(f"  Tax      : ${tax:.2f}")
    click.echo(f"  Total    : ${total:.2f}")

    buyer_order = order.get("order")
    if buyer_order:
        click.echo(f"\nBuyer order ID : {buyer_order.get('id', '?')}")

    # Dump keys we don't recognise so nothing is silently hidden
    known = {"id", "status", "totals", "line_items", "items", "order",
             "shipping_address", "billing_address", "created_at", "updated_at"}
    extra = {k: v for k, v in order.items() if k not in known}
    if extra:
        click.echo(f"\nAdditional fields: {_json.dumps(extra, indent=2)}")


@cli.command("price-update")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--dry-run", "-n", is_flag=True,
              help="Show what would change without applying any updates.")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def price_update(
    ctx: click.Context,
    config_path: Path | None,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Update seller inventory prices based on market and competitor analysis.

    Fetches current ManaPool prices, computes optimal sell prices using
    the configured strategy, and applies updates. Uses cost basis from DB
    to enforce minimum margin, with automatic floor expiry after configured days.
    """
    _configure_logging(verbose)
    from manabot.config import load_config
    from manabot.api.manapool import ManaPoolClient
    from manabot.db import open_db
    from manabot.pricer import run_pricing_update, PricingConfig

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    client = ManaPoolClient(
        email=config.manapool_email,
        token=config.manapool_token,
        use_bulk_export=config.use_bulk_export,
    )
    pricing_cfg = PricingConfig(
        race_to_bottom_threshold=config.pricer_race_to_bottom_threshold,
        min_margin_pct=config.pricer_min_margin_pct,
        cost_floor_days=config.pricer_cost_floor_days,
        iqr_fence_factor=config.pricer_iqr_fence_factor,
        min_sales_for_regression=config.pricer_min_sales_for_regression,
        max_sale_age_days=config.pricer_max_sale_age_days,
        finish_merge_max_price_usd=getattr(config, "pricer_finish_merge_max_price_usd", 2.0),
        finish_merge_threshold_usd=getattr(config, "pricer_finish_merge_threshold_usd", 1.0),
    )

    if dry_run:
        click.echo("[dry-run] Simulating price update — no changes will be applied.")

    with open_db(config.db_path) as conn:
        recommendations = run_pricing_update(client, conn, config, pricing_cfg, dry_run=dry_run)

    if not recommendations:
        click.echo("No seller inventory found.")
        return

    n_update = sum(1 for r in recommendations if r.should_update)
    n_skip = sum(1 for r in recommendations if r.reason == "no_data")
    n_no_change = len(recommendations) - n_update - n_skip

    click.echo(f"\nPricing summary: {len(recommendations)} listing(s)")
    click.echo(f"  {'Would update' if dry_run else 'Updated'}:  {n_update}")
    click.echo(f"  No change:  {n_no_change}")
    click.echo(f"  Skipped (no data): {n_skip}")

    if n_update > 0:
        click.echo(f"\n{'Would update' if dry_run else 'Updated'} listings:")
        for r in sorted(recommendations, key=lambda x: x.card_name):
            if not r.should_update:
                continue
            trend_str = f"trend ${r.trend_target_usd:.2f}" if r.trend_target_usd is not None else "no trend"
            low_str = f"low ${r.low_price_usd:.2f}" if r.low_price_usd else "no listings"
            click.echo(
                f"  {r.card_name[:40]:<40} [{r.set_code}] {r.condition.value}/{r.finish.value}"
                f"  ${r.current_price_usd:.2f} -> ${r.new_price_usd:.2f}"
                f"  ({r.reason}, {trend_str}, {low_str})"
            )


@cli.command("margin-report")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--days", type=int, default=None,
              help="Restrict to sales in the last N days.")
@click.option("--card", "card_filter", type=str, default=None,
              help="Filter by card name (substring match).")
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def margin_report(
    ctx: click.Context,
    config_path: Path | None,
    days: int | None,
    card_filter: str | None,
    verbose: bool,
) -> None:
    """Show P&L report from completed sales tracked in the DB."""
    _configure_logging(verbose)
    from manabot.config import load_config
    from manabot.db import open_db, get_margin_report

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    with open_db(config.db_path) as conn:
        rows = get_margin_report(conn, days=days, card_name_filter=card_filter)

    if not rows:
        period = f" (last {days} days)" if days else ""
        click.echo(f"No sales recorded{period}.")
        return

    total_margin = sum(r["total_margin"] or 0 for r in rows)
    total_sold = sum(r["total_sold"] or 0 for r in rows)

    header = f"{'Card':<42} {'Sold':>6} {'Avg Sell':>9} {'Avg Cost':>9} {'Total Margin':>13} {'Win%':>6}"
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        avg_cost = f"${r['avg_cost']:.2f}" if r["avg_cost"] is not None else "    N/A"
        win_pct = (r["profitable_count"] / r["sale_count"] * 100) if r["sale_count"] else 0.0
        margin_str = f"${r['total_margin']:+.2f}" if r["total_margin"] is not None else "    N/A"
        click.echo(
            f"{r['card_name'][:41]:<42} {r['total_sold']:>6} "
            f"${r['avg_sell_price']:>8.2f} {avg_cost:>9} {margin_str:>13} {win_pct:>5.0f}%"
        )
    click.echo("-" * len(header))
    click.echo(f"{'TOTAL':<42} {total_sold:>6} {'':>9} {'':>9} ${total_margin:>+12.2f}")


@cli.command("import-cost-basis")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--file", "csv_path", required=True, type=click.Path(path_type=Path),
              help="CSV file with cost basis data.")
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def import_cost_basis(
    ctx: click.Context,
    config_path: Path | None,
    csv_path: Path,
    verbose: bool,
) -> None:
    """Import purchase prices into the cost basis DB from a CSV file.

    CSV columns (header required):
      scryfall_id, card_name, set_code, condition, finish, cost_usd, quantity, acquired_at

    condition: NM, LP, MP, HP, DMG
    finish:    nonfoil, foil
    acquired_at: ISO date or datetime (e.g. 2025-01-15)
    """
    _configure_logging(verbose)
    import csv as _csv
    from manabot.config import load_config
    from manabot.db import open_db, set_cost_basis
    from manabot.models import Condition, Finish

    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)

    required_cols = {"scryfall_id", "card_name", "set_code", "condition", "finish", "cost_usd", "quantity", "acquired_at"}
    inserted = 0
    errors = 0

    with open_db(config.db_path) as conn:
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            if not reader.fieldnames or not required_cols.issubset(set(reader.fieldnames)):
                missing = required_cols - set(reader.fieldnames or [])
                click.echo(f"CSV missing required columns: {missing}", err=True)
                sys.exit(1)
            for i, row in enumerate(reader, start=2):
                try:
                    cond = Condition(row["condition"].strip().upper())
                    finish_str = row["finish"].strip().lower()
                    finish = Finish.FOIL if finish_str == "foil" else Finish.NONFOIL
                    acq = datetime.fromisoformat(row["acquired_at"].strip())
                    set_cost_basis(
                        conn,
                        scryfall_id=row["scryfall_id"].strip(),
                        card_name=row["card_name"].strip(),
                        set_code=row["set_code"].strip().upper(),
                        condition=cond,
                        finish=finish,
                        cost_usd=float(row["cost_usd"]),
                        quantity=int(row["quantity"]),
                        acquired_at=acq,
                        source="import",
                    )
                    inserted += 1
                except (ValueError, KeyError) as e:
                    click.echo(f"Row {i}: {e}", err=True)
                    errors += 1

    click.echo(f"Imported {inserted} cost basis record(s). {errors} error(s).")


@cli.command("pricer-scheduler")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def pricer_scheduler(ctx: click.Context, config_path: Path | None) -> None:
    """Start the daily price update scheduler (blocking)."""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    from manabot.config import load_config
    from manabot.scheduler import schedule_daily_price_update
    try:
        config = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Config error: {e}", err=True)
        sys.exit(1)
    schedule_daily_price_update(config)
