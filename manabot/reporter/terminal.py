from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich import box

from manabot.models import MatchResult, MatchStatus, TrendDirection

_TREND_ARROW = {
    TrendDirection.UP: "[red]↑[/red]",
    TrendDirection.DOWN: "[green]↓[/green]",
    TrendDirection.FLAT: "[dim]→[/dim]",
    TrendDirection.NEW: "[dim]NEW[/dim]",
}

_STATUS_LABEL = {
    MatchStatus.MATCHED: "",
    MatchStatus.UNRESOLVED: "[yellow]UNRESOLVED[/yellow]",
    MatchStatus.WARN_SCRYFALL_NEEDED: "[yellow]WARN: no Scryfall[/yellow]",
}


def render(results: list[MatchResult], console: Console | None = None) -> None:
    console = console or Console()

    table = Table(box=box.SIMPLE_HEAVY, show_footer=False)
    table.add_column("Card", style="bold", min_width=20)
    table.add_column("Tags", style="dim")
    table.add_column("Best Price", justify="right")
    table.add_column("Max Price", justify="right")
    table.add_column("Avail", justify="right")
    table.add_column("Trend", justify="center")
    table.add_column("Status")

    for r in results:
        item = r.buy_list_item
        tags = ", ".join(item.tags) if item.tags else ""

        if r.status == MatchStatus.UNRESOLVED:
            table.add_row(
                item.card_name, tags, "—", f"${item.max_price_usd:.2f}", "—", "—",
                _STATUS_LABEL[r.status],
                style="dim",
            )
            continue

        best_str = f"${r.best_price:.2f}" if r.best_price is not None else "—"
        avail = str(r.listings[0].quantity_available) if r.listings else "—"
        trend_str = _TREND_ARROW.get(r.trend.direction, "—") if r.trend else "—"
        status_str = _STATUS_LABEL[r.status]

        if r.is_good_buy:
            row_style = "green"
        elif r.best_price is not None and r.best_price <= item.max_price_usd * 1.10:
            row_style = "yellow"
        else:
            row_style = "red"

        table.add_row(
            item.card_name, tags, best_str, f"${item.max_price_usd:.2f}",
            avail, trend_str, status_str,
            style=row_style,
        )

    console.print(table)
