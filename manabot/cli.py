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
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Manabot — MTG price monitoring bot for ManaPool."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
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
@click.pass_context
def run(
    ctx: click.Context,
    config_path: Path | None,
    buylist_path_override: Path | None,
    dry_run: bool,
    notify_always: bool,
    no_html: bool,
) -> None:
    """Fetch prices, match against buy list, and report results."""
    from manabot.config import load_config
    from manabot.buylist import load_buylist
    from manabot.db import open_db, insert_listings, record_fetch_run
    from manabot.api.manapool import ManaPoolClient
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

        results = match(buy_list, listings)
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
@click.pass_context
def scheduled(ctx: click.Context) -> None:
    """Run on a schedule (requires APScheduler — not yet configured)."""
    from manabot.scheduler import schedule_run
    try:
        schedule_run("", None)
    except NotImplementedError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
