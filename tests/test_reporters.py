import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from manabot.models import (
    BuyListItem,
    Condition,
    Finish,
    MatchResult,
    MatchStatus,
    PriceListing,
    TrendData,
    TrendDirection,
)
from manabot.reporter import terminal, csv_report, discord, html as html_reporter

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
BOLT_ID = "e3285e6b-3e79-4d7c-bf96-d920f973b122"


def make_result(
    card_name="Lightning Bolt",
    best_price=1.25,
    max_price=2.00,
    is_good_buy=True,
    tags=None,
    trend_dir=TrendDirection.DOWN,
    status=MatchStatus.MATCHED,
) -> MatchResult:
    item = BuyListItem(
        card_name=card_name,
        scryfall_id=BOLT_ID,
        target_quantity=4,
        max_price_usd=max_price,
        min_condition=Condition.LP,
        tags=tags or [],
    )
    listing = PriceListing(
        scryfall_id=BOLT_ID,
        card_name=card_name,
        set_code="LEB",
        condition=Condition.NM,
        finish=Finish.NONFOIL,
        price_usd=best_price,
        quantity_available=4,
        seller_id="seller1",
        fetched_at=NOW,
    )
    trend = TrendData(
        scryfall_id=BOLT_ID,
        price_now=best_price,
        price_then=best_price * 1.1,
        direction=trend_dir,
    )
    return MatchResult(
        buy_list_item=item,
        listings=[listing],
        best_price=best_price,
        is_good_buy=is_good_buy,
        trend=trend,
        status=status,
    )


# --- Terminal reporter ---

def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    # markup=True so Rich processes style tags; no_color strips ANSI codes; wide width avoids truncation
    console = Console(file=buf, highlight=False, markup=True, no_color=True, width=200)
    return console, buf


def test_terminal_renders_card_name():
    console, buf = _console()
    terminal.render([make_result()], console=console)
    assert "Lightning Bolt" in buf.getvalue()


def test_terminal_renders_price():
    console, buf = _console()
    terminal.render([make_result(best_price=1.25)], console=console)
    assert "1.25" in buf.getvalue()


def test_terminal_renders_unresolved():
    console, buf = _console()
    r = make_result(status=MatchStatus.UNRESOLVED)
    r.listings = []
    r.best_price = None
    r.is_good_buy = False
    terminal.render([r], console=console)
    assert "UNRESOLVED" in buf.getvalue()


def test_terminal_renders_tags():
    console, buf = _console()
    terminal.render([make_result(tags=["burn-deck", "customer:John"])], console=console)
    output = buf.getvalue()
    assert "burn-deck" in output


# --- CSV reporter ---

def test_csv_report_creates_file(tmp_path):
    path = csv_report.write([make_result()], tmp_path, NOW)
    assert path.exists()


def test_csv_report_contains_card_name(tmp_path):
    path = csv_report.write([make_result()], tmp_path, NOW)
    content = path.read_text()
    assert "Lightning Bolt" in content


def test_csv_report_contains_status(tmp_path):
    path = csv_report.write([make_result()], tmp_path, NOW)
    content = path.read_text()
    assert "MATCHED" in content


def test_csv_report_good_buy_flag(tmp_path):
    r = make_result(is_good_buy=True)
    path = csv_report.write([r], tmp_path, NOW)
    content = path.read_text()
    assert "yes" in content


# --- HTML reporter ---

def test_html_report_creates_file(tmp_path):
    summary = {"good_buy_count": 1, "total_checked": 1, "unresolved_count": 0, "warn_scryfall_count": 0}
    path = html_reporter.write([make_result()], tmp_path, NOW, summary)
    assert path.exists()
    assert path.suffix == ".html"


def test_html_report_contains_card_name(tmp_path):
    summary = {"good_buy_count": 1, "total_checked": 1, "unresolved_count": 0, "warn_scryfall_count": 0}
    path = html_reporter.write([make_result()], tmp_path, NOW, summary)
    assert "Lightning Bolt" in path.read_text()


def test_html_report_contains_price(tmp_path):
    summary = {"good_buy_count": 1, "total_checked": 1, "unresolved_count": 0, "warn_scryfall_count": 0}
    path = html_reporter.write([make_result(best_price=1.25)], tmp_path, NOW, summary)
    assert "1.25" in path.read_text()


# --- Discord reporter ---

def test_discord_dry_run_prints_payload(capsys):
    summary = {"good_buy_count": 1, "total_checked": 4, "unresolved_count": 0, "warn_scryfall_count": 0}
    discord.send([make_result()], "https://discord.com/api/webhooks/fake", summary, NOW, dry_run=True)
    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out


def test_discord_dry_run_payload_structure(capsys):
    summary = {"good_buy_count": 1, "total_checked": 4, "unresolved_count": 0, "warn_scryfall_count": 0}
    discord.send([make_result()], "https://discord.com/api/webhooks/fake", summary, NOW, dry_run=True)
    captured = capsys.readouterr()
    # Extract the JSON payload from output
    json_str = captured.out.split("[dry-run] Discord payload:\n", 1)[1]
    payload = json.loads(json_str)
    assert "embeds" in payload
    assert payload["embeds"][0]["title"] == "Manabot — 1 good buy found"


def test_discord_skips_when_no_webhook(capsys):
    summary = {"good_buy_count": 0, "total_checked": 4, "unresolved_count": 0, "warn_scryfall_count": 0}
    discord.send([], "", summary, NOW, dry_run=False)
    # Should not raise; just logs and returns


def test_discord_shows_top_5_only(capsys):
    results = [make_result(card_name=f"Card {i}") for i in range(8)]
    summary = {"good_buy_count": 8, "total_checked": 8, "unresolved_count": 0, "warn_scryfall_count": 0}
    discord.send(results, "https://discord.com/api/webhooks/fake", summary, NOW, dry_run=True)
    captured = capsys.readouterr()
    json_str = captured.out.split("[dry-run] Discord payload:\n", 1)[1]
    payload = json.loads(json_str)
    assert len(payload["embeds"][0]["fields"]) == 5
