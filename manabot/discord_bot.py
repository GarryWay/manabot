"""Discord slash-command bot for manabot.

Commands
--------
/run              Fetch prices and show good buys from the buy list
/optimize         Run the ManaPool optimizer and show the best cart
/arbitrage        Find listings trading below market value
/add-card         Add a single card to the buy list (tagged with your username + Discord ID)
/add-cards        Add multiple cards at once (one per line, CSV format)
/buylist          Display the current buy list, with optional tag filter (e.g. user:Garrett)
/mark-purchased   Remove purchased cards; pings the Discord user who added each card
/remove-card      Remove a buy list entry you added (force=True to remove any entry)
/edit-card        Edit quantity, price, condition, or set restriction on your entry

Parameters of note:
  /optimize margin_pct     — require cards to be X% below max price (replaces over_budget_pct)
  /optimize target_cart_usd — initial budget before free-rider expansion
  /optimize|/arbitrage max_iterations — override removal trial count for large carts

Install deps:  pip install ".[full]"
Start the bot: python -m manabot bot
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

from manabot.config import Config
from manabot.models import BuyListItem, Condition, Finish

log = logging.getLogger(__name__)

_VALID_CONDITIONS = [c.value for c in Condition]
_VALID_FINISHES = [f.value for f in Finish]

_SCRYFALL_REFRESH_INTERVAL = 7 * 24 * 3600  # weekly


async def _scryfall_refresh_loop(oracle_path: Path) -> None:
    """Download oracle data immediately, then refresh weekly."""
    from manabot.api.scryfall_bulk import download_oracle_cards
    while True:
        try:
            downloaded = await asyncio.to_thread(download_oracle_cards, oracle_path)
            log.info("Scryfall oracle: %s", "updated" if downloaded else "already current")
        except Exception as exc:
            log.warning("Scryfall oracle refresh failed: %s", exc)
        await asyncio.sleep(_SCRYFALL_REFRESH_INTERVAL)


# ── Last-cart persistence ────────────────────────────────────────────────────

def _last_cart_path(config: Config) -> Path:
    return config.db_path.parent / "last_cart.json"


def _save_last_cart(config: Config, items: list[dict], command: str) -> None:
    path = _last_cart_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "command": command, "items": items}, indent=2),
        encoding="utf-8",
    )


def _load_last_cart(config: Config) -> list[dict] | None:
    path = _last_cart_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("items")
    except Exception:
        return None


# ── Discord message helpers ──────────────────────────────────────────────────

def _send_as_file_or_text(text: str, filename: str) -> tuple[str | None, discord.File | None]:
    """Return (content, file) — use a file attachment when text exceeds Discord's limit."""
    if len(text) <= 1950:
        return f"```\n{text}\n```", None
    return None, discord.File(io.BytesIO(text.encode()), filename=filename)


def _send_kwargs(
    embed: discord.Embed,
    content: str | None = None,
    file: discord.File | None = None,
) -> dict:
    """Build followup.send kwargs, omitting None values (discord.py uses MISSING, not None)."""
    kw: dict = {"embed": embed}
    if content is not None:
        kw["content"] = content
    if file is not None:
        kw["file"] = file
    return kw


# ── Synchronous pipeline helpers (run in thread via asyncio.to_thread) ───────

def _run_pipeline(config: Config) -> dict:
    from manabot.buylist import load_buylist
    from manabot.db import open_db, insert_listings, record_fetch_run
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall import ScryfallClient
    from manabot.matcher import match
    from manabot.analyzer import analyze, summarize

    buy_list = load_buylist(config.buylist_path)
    scryfall_client = ScryfallClient()
    client = ManaPoolClient(email=config.manapool_email, token=config.manapool_token, use_bulk_export=config.use_bulk_export)

    started_at = datetime.now(timezone.utc)
    listings = client.get_singles_prices()

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)
        results = match(buy_list, listings, scryfall_client=scryfall_client)
        results = analyze(results, conn, config.trend_window_days, config.trend_threshold_pct)
        summary = summarize(results)
        completed_at = datetime.now(timezone.utc)
        record_fetch_run(conn, started_at, completed_at, len(listings), summary["good_buy_count"])

    good_buys = [r for r in results if r.is_good_buy]
    return {
        "good_buys": [
            {"card_name": r.buy_list_item.card_name, "quantity": r.buy_list_item.target_quantity,
             "price": r.best_price, "max_price": r.buy_list_item.max_price_usd}
            for r in good_buys
        ],
        "good_buy_count": summary["good_buy_count"],
        "total_checked": summary["total_checked"],
        "unresolved_count": summary["unresolved_count"],
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _optimize_pipeline(
    config: Config,
    over_budget_pct: float,
    max_cart_usd: float | None,
    arb_riders: bool,
    exclude_preorder: bool,
    forced_card_names: frozenset[str] | None = None,
    target_cart_usd: float | None = None,
    max_iterations: int | None = None,
) -> dict:
    from manabot.buylist import load_buylist
    from manabot.db import open_db, insert_listings
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall import ScryfallClient
    from manabot.api.scryfall_bulk import ScryfallBulk
    from manabot.matcher import match
    from manabot.analyzer import analyze
    from manabot.models import MatchStatus
    import manabot.optimizer as opt

    buy_list = load_buylist(config.buylist_path)
    scryfall_bulk = ScryfallBulk()
    client = ManaPoolClient(email=config.manapool_email, token=config.manapool_token, use_bulk_export=config.use_bulk_export)
    listings = client.get_singles_prices()

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)
        results = match(buy_list, listings, scryfall_client=ScryfallClient())
        results = analyze(results, conn, config.trend_window_days, config.trend_threshold_pct)

    matched_count = sum(1 for r in results if r.status == MatchStatus.MATCHED)
    eligible = opt.build_request_items(results, over_budget_pct, scryfall=scryfall_bulk if scryfall_bulk.available else None)
    if not eligible:
        return {"error": f"No eligible items (over_budget_pct={over_budget_pct:.0f}%)"}

    arb_expansion = None
    if arb_riders:
        import manabot.arbitrage as arb
        arb_candidates = arb.find_candidates(
            listings, scryfall=scryfall_bulk if scryfall_bulk.available else None,
            min_discount_pct=10.0, min_quantity=20,
            min_market_price_usd=config.arbitrage_min_market_price_usd,
        )
        if arb_candidates:
            arb_results = arb.candidates_to_match_results(arb_candidates)
            arb_expansion = opt.build_request_items(arb_results, over_budget_pct=0.0)

    effective_max_iter = max_iterations if max_iterations is not None else config.optimizer_max_iterations
    cart = opt.find_best_cart(
        results, client,
        over_budget_pct=over_budget_pct,
        target_cart_usd=target_cart_usd if target_cart_usd is not None else config.optimizer_target_cart_usd,
        max_cart_usd=max_cart_usd,
        max_iterations=effective_max_iter,
        destination_country=config.optimizer_destination,
        scryfall=scryfall_bulk if scryfall_bulk.available else None,
        exclude_preorder=exclude_preorder,
        forced_card_names=forced_card_names,
        expansion_pool=arb_expansion,
    )
    if cart is None:
        return {"error": "Optimizer returned no result"}

    items_data = [
        {"card_name": x.buy_list_item.card_name, "quantity": x.buy_list_item.target_quantity,
         "set_code": x.set_code, "price": x.estimated_price,
         "max_price": x.buy_list_item.max_price_usd, "margin": x.estimated_margin}
        for x in cart.items
    ]
    return {
        "items": items_data,
        "subtotal": cart.subtotal_usd, "shipping": cart.shipping_usd,
        "fees": cart.fees_usd, "total": cart.total_usd,
        "value_budget": cart.value_budget_usd, "net_value": cart.net_value_usd,
        "is_profitable": cart.is_profitable, "matched_count": matched_count,
    }


def _arbitrage_pipeline(
    config: Config,
    min_discount_pct: float,
    min_quantity: int,
    max_cart_usd: float | None,
    target_cart_usd: float | None = None,
    max_iterations: int | None = None,
) -> dict:
    from manabot.db import open_db, insert_listings
    from manabot.api.manapool import ManaPoolClient
    from manabot.api.scryfall_bulk import ScryfallBulk
    import manabot.optimizer as opt
    import manabot.arbitrage as arb

    scryfall = ScryfallBulk()
    client = ManaPoolClient(email=config.manapool_email, token=config.manapool_token, use_bulk_export=config.use_bulk_export)
    listings = client.get_singles_prices()

    with open_db(config.db_path) as conn:
        insert_listings(conn, listings)

    candidates = arb.find_candidates(
        listings, scryfall=scryfall if scryfall.available else None,
        min_discount_pct=min_discount_pct, min_quantity=min_quantity,
        min_market_price_usd=config.arbitrage_min_market_price_usd,
    )
    if not candidates:
        return {"error": f"No arbitrage candidates (≥{min_discount_pct:.0f}% below market)"}

    budget_remaining = (max_cart_usd * 0.65) if max_cart_usd is not None else float("inf")
    selected: list = []
    for c in candidates:
        if c.listing.price_usd > budget_remaining:
            continue
        selected.append(c)
        budget_remaining -= c.listing.price_usd
        if budget_remaining < 0.50:
            break

    if not selected:
        return {"error": "No candidates fit within budget"}

    match_results = arb.candidates_to_match_results(selected)
    prebuilt = opt.build_request_items(match_results, over_budget_pct=0.0)
    if not prebuilt:
        return {"error": "No eligible items after optimizer filtering"}

    effective_max_iter = max_iterations if max_iterations is not None else config.optimizer_max_iterations
    cart = opt.find_best_cart(
        match_results, client,
        over_budget_pct=0.0,
        target_cart_usd=target_cart_usd if target_cart_usd is not None else config.optimizer_target_cart_usd,
        max_cart_usd=max_cart_usd,
        max_iterations=effective_max_iter,
        destination_country=config.optimizer_destination,
        preselected=prebuilt,
    )
    if cart is None:
        return {"error": "Optimizer returned no result"}

    items_data = [
        {"card_name": x.buy_list_item.card_name, "quantity": x.buy_list_item.target_quantity,
         "set_code": x.set_code, "price": x.estimated_price,
         "market_price": x.buy_list_item.max_price_usd,
         "discount_pct": (x.estimated_margin / x.buy_list_item.max_price_usd * 100) if x.buy_list_item.max_price_usd else 0}
        for x in cart.items
    ]
    return {
        "items": items_data, "candidate_count": len(candidates),
        "subtotal": cart.subtotal_usd, "shipping": cart.shipping_usd,
        "fees": cart.fees_usd, "total": cart.total_usd, "net_value": cart.net_value_usd,
    }


# ── Bot factory ──────────────────────────────────────────────────────────────

class _ManabotClient(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True  # needed for guild.get_member() to resolve all guild members
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self._oracle_path = config.db_path.parent / "scryfall_oracle.json"
        self._scryfall_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.discord_guild_id) if self.config.discord_guild_id else None
        if guild:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %d.", self.config.discord_guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour to propagate).")

    async def on_ready(self) -> None:
        log.info("Discord bot ready: %s (ID %s)", self.user, self.user.id)  # type: ignore[union-attr]
        # Start (or restart after reconnect) the weekly Scryfall refresh task.
        if self._scryfall_task is None or self._scryfall_task.done():
            self._scryfall_task = asyncio.create_task(
                _scryfall_refresh_loop(self._oracle_path)
            )


def create_bot(config: Config) -> _ManabotClient:
    """Build and return the configured Discord client. Call bot.run(token) to start."""
    bot = _ManabotClient(config)
    tree = bot.tree

    # ── /run ─────────────────────────────────────────────────────────────────

    @tree.command(name="run", description="Fetch prices and show good buys from your buy list")
    async def cmd_run(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            data = await asyncio.to_thread(_run_pipeline, bot.config)
        except Exception as e:
            log.exception("run pipeline error")
            await interaction.followup.send(f"Pipeline error: {e}")
            return

        count = data["good_buy_count"]
        good_buys = data["good_buys"]
        color = 0x57F287 if count > 0 else 0x95A5A6

        embed = discord.Embed(
            title=f"Manabot — {count} good {'buy' if count == 1 else 'buys'} found",
            color=color,
        )
        for item in good_buys[:10]:
            embed.add_field(
                name=item["card_name"],
                value=f"${item['price']:.2f} / max ${item['max_price']:.2f}",
                inline=True,
            )
        if count > 10:
            embed.add_field(name="…", value=f"and {count - 10} more (see list below)", inline=False)
        embed.set_footer(text=f"Checked {data['total_checked']} · {data['unresolved_count']} unresolved · {data['completed_at']}")

        list_lines = [f"{i['quantity']}x {i['card_name']}  ${i['price']:.2f}/ea" for i in good_buys]
        content, file = _send_as_file_or_text("\n".join(list_lines), "good_buys.txt") if list_lines else (None, None)
        await interaction.followup.send(**_send_kwargs(embed, content, file))

    # ── /optimize ─────────────────────────────────────────────────────────────

    @tree.command(name="optimize", description="Find the best-value cart using the ManaPool optimizer")
    @app_commands.describe(
        margin_pct="Require cards to be at least X% below your max price (default 0; negative allows going over budget)",
        target_cart_usd="Build to this subtotal first, then look for free-rider opportunities (0 = no target)",
        max_cart_usd="Hard spending cap in USD (0 = no cap)",
        max_iterations="Optimizer removal trials — increase for large carts (0 = use config default)",
        arb_riders="Pad cart with arbitrage free-riders from existing sellers",
        exclude_preorder="Exclude pre-order listings (default True)",
        force_cards="Pipe-separated card names to force into the cart regardless of margin (e.g. Counterspell|Sauron, the Dark Lord)",
    )
    async def cmd_optimize(
        interaction: discord.Interaction,
        margin_pct: float = 0.0,
        target_cart_usd: float = 0.0,
        max_cart_usd: float = 0.0,
        max_iterations: int = 0,
        arb_riders: bool = False,
        exclude_preorder: bool = True,
        force_cards: str = "",
    ) -> None:
        await interaction.response.defer(thinking=True)
        # margin_pct is user-facing (positive = require discount); optimizer uses over_budget_pct (inverted sign)
        over_budget_pct = -margin_pct
        max_cart = max_cart_usd if max_cart_usd > 0 else None
        target_cart = target_cart_usd if target_cart_usd > 0 else None
        max_iter = max_iterations if max_iterations > 0 else None
        forced = frozenset(c.strip() for c in force_cards.split("|") if c.strip()) if force_cards else None
        try:
            data = await asyncio.to_thread(
                _optimize_pipeline, bot.config, over_budget_pct, max_cart, arb_riders, exclude_preorder,
                forced, target_cart, max_iter,
            )
        except Exception as e:
            log.exception("optimize pipeline error")
            await interaction.followup.send(f"Optimizer error: {e}")
            return

        if "error" in data:
            await interaction.followup.send(f"No results: {data['error']}")
            return

        _save_last_cart(bot.config, data["items"], "optimize")

        items = data["items"]
        color = 0x57F287 if data["is_profitable"] else 0xE74C3C
        embed = discord.Embed(title=f"Optimized cart — {len(items)} item(s)", color=color)
        embed.add_field(name="Subtotal", value=f"${data['subtotal']:.2f}", inline=True)
        embed.add_field(name="Shipping", value=f"${data['shipping']:.2f}", inline=True)
        embed.add_field(name="Fees", value=f"${data['fees']:.2f}", inline=True)
        embed.add_field(name="Total", value=f"${data['total']:.2f}", inline=True)
        embed.add_field(name="Budget", value=f"${data['value_budget']:.2f}", inline=True)
        embed.add_field(
            name="Net value",
            value=f"${data['net_value']:+.2f}  {'✓ profitable' if data['is_profitable'] else '⚠ over budget'}",
            inline=True,
        )
        embed.set_footer(text="Cart saved — use /mark-purchased after completing your order.")

        lines = []
        for x in items:
            sign = "+" if x["margin"] >= 0 else "-"
            lines.append(f"{x['quantity']}x {x['card_name']} [{x['set_code']}]  ${x['price']:.2f}/ea  ({sign}${abs(x['margin']):.2f})")
        content, file = _send_as_file_or_text("\n".join(lines), "cart.txt")
        await interaction.followup.send(**_send_kwargs(embed, content, file))

    # ── /arbitrage ────────────────────────────────────────────────────────────

    @tree.command(name="arbitrage", description="Find ManaPool listings trading below market value")
    @app_commands.describe(
        min_discount_pct="Minimum % below market price (default 10)",
        min_quantity="Minimum available quantity (default 20)",
        target_cart_usd="Build to this subtotal first, then look for free-rider opportunities (0 = no target)",
        max_cart_usd="Hard spending cap in USD (0 = no cap)",
        max_iterations="Optimizer removal trials — increase for large carts (0 = use config default)",
    )
    async def cmd_arbitrage(
        interaction: discord.Interaction,
        min_discount_pct: float = 10.0,
        min_quantity: int = 20,
        target_cart_usd: float = 0.0,
        max_cart_usd: float = 0.0,
        max_iterations: int = 0,
    ) -> None:
        await interaction.response.defer(thinking=True)
        max_cart = max_cart_usd if max_cart_usd > 0 else None
        target_cart = target_cart_usd if target_cart_usd > 0 else None
        max_iter = max_iterations if max_iterations > 0 else None
        try:
            data = await asyncio.to_thread(
                _arbitrage_pipeline, bot.config, min_discount_pct, min_quantity, max_cart, target_cart, max_iter,
            )
        except Exception as e:
            log.exception("arbitrage pipeline error")
            await interaction.followup.send(f"Arbitrage error: {e}")
            return

        if "error" in data:
            await interaction.followup.send(f"No results: {data['error']}")
            return

        _save_last_cart(bot.config, data["items"], "arbitrage")

        items = data["items"]
        color = 0x57F287 if data["net_value"] > 0 else 0xE74C3C
        embed = discord.Embed(
            title=f"Arbitrage cart — {len(items)} item(s)",
            description=f"{data['candidate_count']} candidates found (≥{min_discount_pct:.0f}% below market)",
            color=color,
        )
        embed.add_field(name="Subtotal", value=f"${data['subtotal']:.2f}", inline=True)
        embed.add_field(name="Shipping", value=f"${data['shipping']:.2f}", inline=True)
        embed.add_field(name="Fees", value=f"${data['fees']:.2f}", inline=True)
        embed.add_field(name="Total", value=f"${data['total']:.2f}", inline=True)
        embed.add_field(name="Net value (resale)", value=f"${data['net_value']:+.2f}", inline=True)
        embed.set_footer(text="Cart saved — use /mark-purchased after completing your order.")

        lines = []
        for x in items:
            lines.append(
                f"{x['quantity']}x {x['card_name']} [{x['set_code']}]"
                f"  ${x['price']:.2f}/ea  (market ${x['market_price']:.2f}, -{x['discount_pct']:.1f}%)"
            )
        content, file = _send_as_file_or_text("\n".join(lines), "arbitrage_cart.txt")
        await interaction.followup.send(**_send_kwargs(embed, content, file))

    # ── /add-card ─────────────────────────────────────────────────────────────

    @tree.command(name="add-card", description="Add a single card to the buy list")
    @app_commands.describe(
        card_name="Card name, e.g. Lightning Bolt",
        quantity="Number of copies to buy",
        max_price="Maximum price per copy in USD",
        condition="Minimum acceptable condition (default NM)",
        set_code="Restrict to a specific set code, e.g. LEA (optional)",
        foil="Foil preference: any, nonfoil, or foil (default any)",
    )
    @app_commands.choices(
        condition=[app_commands.Choice(name=c, value=c) for c in _VALID_CONDITIONS],
        foil=[app_commands.Choice(name=f, value=f) for f in _VALID_FINISHES],
    )
    async def cmd_add_card(
        interaction: discord.Interaction,
        card_name: str,
        quantity: int,
        max_price: float,
        condition: str = "NM",
        set_code: str = "",
        foil: str = "any",
    ) -> None:
        from manabot.buylist import append_to_buylist

        username = interaction.user.display_name
        allowed_sets = [set_code.strip().upper()] if set_code.strip() else []
        item = BuyListItem(
            card_name=card_name.strip(),
            target_quantity=quantity,
            max_price_usd=max_price,
            min_condition=Condition(condition),
            foil=Finish(foil),
            allowed_sets=allowed_sets,
            tags=[f"user:{username}", f"uid:{interaction.user.id}"],
        )
        try:
            append_to_buylist(bot.config.buylist_path, item)
        except Exception as e:
            await interaction.response.send_message(f"Error adding card: {e}", ephemeral=True)
            return

        set_str = f" [{set_code.upper()}]" if set_code.strip() else ""
        await interaction.response.send_message(
            f"Added **{quantity}x {card_name}{set_str}** to the buy list "
            f"(max ${max_price:.2f}, {condition}, {foil})."
        )

    # ── /add-cards ────────────────────────────────────────────────────────────

    @tree.command(
        name="add-cards",
        description="Add multiple cards to the buy list — one per line: name,qty,price[,condition[,set[,foil]]]",
    )
    @app_commands.describe(
        cards="Each line: card_name,quantity,max_price[,condition[,set_code[,foil]]]\nExample: Lightning Bolt,4,1.50,LP"
    )
    async def cmd_add_cards(interaction: discord.Interaction, cards: str) -> None:
        from manabot.buylist import append_to_buylist

        username = interaction.user.display_name
        added: list[str] = []
        errors: list[str] = []

        for line_num, raw in enumerate(cards.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                parts = [p.strip() for p in next(csv.reader([line]))]
            except Exception:
                errors.append(f"Line {line_num}: could not parse — got: {line!r}")
                continue
            if len(parts) < 3:
                errors.append(f"Line {line_num}: need name,qty,price — got: {line!r}")
                continue
            card_name = parts[0]
            if not card_name:
                errors.append(f"Line {line_num}: card_name is empty")
                continue
            try:
                qty = int(parts[1])
                price = float(parts[2])
            except ValueError:
                errors.append(f"Line {line_num} ({card_name!r}): qty must be int, price must be float")
                continue

            cond_str = parts[3].strip().upper() if len(parts) > 3 and parts[3].strip() else "NM"
            if cond_str not in {c.value for c in Condition}:
                errors.append(f"Line {line_num} ({card_name!r}): invalid condition {cond_str!r}")
                continue
            set_str = parts[4].strip().upper() if len(parts) > 4 and parts[4].strip() else ""
            foil_str = parts[5].strip().lower() if len(parts) > 5 and parts[5].strip() else "any"
            if foil_str not in {f.value for f in Finish}:
                errors.append(f"Line {line_num} ({card_name!r}): invalid foil {foil_str!r}")
                continue

            item = BuyListItem(
                card_name=card_name,
                target_quantity=qty,
                max_price_usd=price,
                min_condition=Condition(cond_str),
                foil=Finish(foil_str),
                allowed_sets=[set_str] if set_str else [],
                tags=[f"user:{username}", f"uid:{interaction.user.id}"],
            )
            try:
                append_to_buylist(bot.config.buylist_path, item)
                set_label = f" [{set_str}]" if set_str else ""
                added.append(f"{qty}x {card_name}{set_label}  max ${price:.2f}  {cond_str}")
            except Exception as e:
                errors.append(f"Line {line_num} ({card_name!r}): {e}")

        parts_msg: list[str] = []
        if added:
            parts_msg.append(f"Added {len(added)} card(s):\n" + "\n".join(f"  • {a}" for a in added))
        if errors:
            parts_msg.append(f"{len(errors)} error(s):\n" + "\n".join(f"  ✗ {e}" for e in errors))

        msg = "\n\n".join(parts_msg) or "Nothing to add."
        await interaction.response.send_message(msg[:2000])

    # ── /buylist ──────────────────────────────────────────────────────────────

    @tree.command(name="buylist", description="Display the current buy list")
    @app_commands.describe(tag="Filter by tag, e.g. user:Garrett")
    async def cmd_buylist(interaction: discord.Interaction, tag: str = "") -> None:
        from manabot.buylist import load_buylist

        try:
            items = load_buylist(bot.config.buylist_path)
        except FileNotFoundError:
            await interaction.response.send_message("Buy list file not found.", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(f"Error loading buy list: {e}", ephemeral=True)
            return

        if tag:
            items = [i for i in items if tag.lower() in {t.lower() for t in i.tags}]

        if not items:
            suffix = f" matching tag {tag!r}" if tag else ""
            await interaction.response.send_message(f"No items in buy list{suffix}.")
            return

        lines = []
        for item in items:
            set_str = f" [{','.join(item.allowed_sets)}]" if item.allowed_sets else ""
            foil_str = f" {item.foil.value}" if item.foil != Finish.ANY else ""
            tags_str = f"  [{','.join(item.tags)}]" if item.tags else ""
            lines.append(
                f"{item.target_quantity}x {item.card_name}{set_str}{foil_str}"
                f"  max ${item.max_price_usd:.2f}  {item.min_condition.value}{tags_str}"
            )

        header = f"Buy list — {len(items)} item(s){f' (tag: {tag})' if tag else ''}:"
        body = "\n".join(lines)
        content, file = _send_as_file_or_text(body, "buylist.txt")
        if file:
            await interaction.response.send_message(header, file=file)
        else:
            await interaction.response.send_message(f"{header}\n{content}")

    # ── /mark-purchased ───────────────────────────────────────────────────────

    @tree.command(
        name="mark-purchased",
        description="Remove purchased cards from the buy list after a ManaPool order",
    )
    @app_commands.describe(
        cards=(
            "Comma-separated card names to remove (removes all copies FIFO). "
            "Leave blank to use quantities from the most recent /optimize or /arbitrage run."
        ),
    )
    async def cmd_mark_purchased(interaction: discord.Interaction, cards: str = "") -> None:
        from manabot.buylist import remove_purchases_fifo

        if cards.strip():
            # Manual list — remove all copies of each named card, FIFO
            purchases: list[tuple[str, int]] = [
                (n.strip(), -1) for n in cards.split(",") if n.strip()
            ]
        else:
            last = _load_last_cart(bot.config)
            if last is None:
                await interaction.response.send_message(
                    "No recent run found. Provide card names or run `/optimize` first.",
                    ephemeral=True,
                )
                return
            # Use exact quantities from the last cart, consuming FIFO
            purchases = [(item["card_name"], item["quantity"]) for item in last]

        try:
            affected = await asyncio.to_thread(
                remove_purchases_fifo, bot.config.buylist_path, purchases
            )
        except Exception as e:
            await interaction.response.send_message(f"Error updating buy list: {e}", ephemeral=True)
            return

        if not affected:
            all_names = [p[0] for p in purchases]
            # Arb riders from /optimize arb_riders=True or /arbitrage are not in the
            # buy list — that's expected. Give a clearer message when the cart command
            # was arbitrage-sourced.
            last_meta_path = _last_cart_path(bot.config)
            try:
                last_meta = json.loads(last_meta_path.read_text(encoding="utf-8")) if last_meta_path.exists() else {}
            except Exception:
                last_meta = {}
            arb_hint = " (arb carts don't touch the buy list)" if last_meta.get("command") == "arbitrage" else ""
            await interaction.response.send_message(
                "No matching cards found in buy list for: "
                + ", ".join(all_names[:10])
                + (f" ... and {len(all_names) - 10} more" if len(all_names) > 10 else "")
                + arb_hint
            )
            return

        # Summarise what was consumed
        total_qty = sum(int(r.get("qty_purchased", "1")) for r in affected)
        unique_cards = len({r.get("card_name", "") for r in affected})
        lines = []
        for r in affected:
            qty_purchased = int(r.get("qty_purchased", "1") or "1")
            qty_original = int(r.get("target_quantity", "1") or "1")
            partial = qty_purchased < qty_original
            lines.append(f"{r['qty_purchased']}x {r['card_name']}" + (" (partial)" if partial else ""))
        summary = (
            f"Marked **{total_qty}** cop{'y' if total_qty == 1 else 'ies'} "
            f"across **{unique_cards}** card(s) as purchased:\n"
            + "\n".join(f"  • {l}" for l in lines[:30])
            + (f"\n  ... and {len(lines) - 30} more" if len(lines) > 30 else "")
        )

        # Ping each Discord user whose entries were affected (once per unique uid).
        # Tags are stored as "user:Name,uid:SNOWFLAKE" — extract both for the message
        # so it's readable even when the Discord client hasn't loaded the mention yet.
        uid_to_name: dict[str, str] = {}
        for row in affected:
            name_tag = ""
            uid_tag = ""
            for tag in (row.get("tags") or "").split(","):
                tag = tag.strip()
                if tag.startswith("user:"):
                    name_tag = tag[5:].strip()
                elif tag.startswith("uid:"):
                    uid_tag = tag[4:].strip()
            if uid_tag and uid_tag.isdigit():
                uid_to_name.setdefault(uid_tag, name_tag or uid_tag)

        if uid_to_name:
            mention_parts = []
            for uid, name in sorted(uid_to_name.items()):
                member = interaction.guild.get_member(int(uid)) if interaction.guild else None
                if member:
                    mention_parts.append(member.mention)
                else:
                    mention_parts.append(f"<@{uid}> ({name})")
            summary += f"\n\nFYI {' '.join(mention_parts)} — your cards have been purchased."

        await interaction.response.send_message(summary[:2000])

    # ── /remove-card ──────────────────────────────────────────────────────────

    @tree.command(name="remove-card", description="Remove a buy list entry you added (or any entry with force)")
    @app_commands.describe(
        card_name="Card name to remove",
        force="Remove any matching entry, not just your own",
    )
    async def cmd_remove_card(
        interaction: discord.Interaction,
        card_name: str,
        force: bool = False,
    ) -> None:
        from manabot.buylist import remove_purchases_fifo

        caller_uid = str(interaction.user.id)
        uid_filter = None if force else caller_uid

        await interaction.response.defer()

        try:
            affected = await asyncio.to_thread(
                remove_purchases_fifo,
                bot.config.buylist_path,
                [(card_name.strip(), -1)],
                uid_filter,
            )
        except Exception as e:
            await interaction.followup.send(f"Error updating buy list: {e}", ephemeral=True)
            return

        if not affected:
            qualifier = "any entry" if force else "your entries"
            await interaction.followup.send(
                f"No {qualifier} found in buy list for **{card_name}**.",
                ephemeral=True,
            )
            return

        total_qty = sum(int(r.get("qty_purchased", "1")) for r in affected)
        msg = (
            f"Removed **{total_qty}x {card_name}** "
            f"({len(affected)} row(s)) from the buy list."
        )

        # If force-removed someone else's entry, ping the owner
        if force:
            other_uids = {
                tag[4:].strip()
                for row in affected
                for tag in (row.get("tags") or "").split(",")
                if tag.strip().startswith("uid:") and tag.strip()[4:] != caller_uid
            }
            if other_uids:
                mentions = " ".join(f"<@{u}>" for u in sorted(other_uids))
                msg += f"\n\nFYI {mentions} — your entry was removed by {interaction.user.display_name}."

        await interaction.followup.send(msg)

    # ── /edit-card ────────────────────────────────────────────────────────────

    @tree.command(name="edit-card", description="Edit an existing buy list entry (yours, or any with force)")
    @app_commands.describe(
        card_name="Card name to find and edit",
        quantity="New quantity (0 = keep current)",
        max_price="New max price in USD (0 = keep current)",
        condition="New minimum condition",
        set_code="New set restriction (pass 'any' to clear it)",
        foil="New foil preference",
        force="Edit any matching entry, not just your own",
    )
    @app_commands.choices(
        condition=[app_commands.Choice(name=c, value=c) for c in _VALID_CONDITIONS],
        foil=[app_commands.Choice(name=f, value=f) for f in _VALID_FINISHES],
    )
    async def cmd_edit_card(
        interaction: discord.Interaction,
        card_name: str,
        quantity: int = 0,
        max_price: float = 0.0,
        condition: str = "",
        set_code: str = "",
        foil: str = "",
        force: bool = False,
    ) -> None:
        from manabot.buylist import edit_buylist_entry

        if not any([quantity, max_price, condition, set_code, foil]):
            await interaction.response.send_message(
                "Nothing to change — provide at least one field to update.",
                ephemeral=True,
            )
            return

        caller_uid = str(interaction.user.id)
        uid_filter = None if force else caller_uid

        # Build CSV-field updates (None = keep current)
        updates: dict[str, str | None] = {
            "target_quantity": str(quantity) if quantity > 0 else None,
            "max_price_usd": str(max_price) if max_price > 0 else None,
            "min_condition": condition if condition else None,
            "allowed_sets": ("" if set_code.strip().lower() in ("any", "clear", "") and set_code.strip()
                             else set_code.strip().upper() if set_code.strip() else None),
            "foil": foil if foil else None,
        }

        await interaction.response.defer()

        try:
            original = await asyncio.to_thread(
                edit_buylist_entry,
                bot.config.buylist_path,
                card_name.strip(),
                updates,
                uid_filter,
            )
        except Exception as e:
            await interaction.followup.send(f"Error updating buy list: {e}", ephemeral=True)
            return

        if original is None:
            qualifier = "any entry" if force else "your entry"
            await interaction.followup.send(
                f"No {qualifier} found in buy list for **{card_name}**.",
                ephemeral=True,
            )
            return

        def _fmt_row(r: dict[str, str]) -> str:
            sets = r.get("allowed_sets", "")
            set_str = f" [{sets}]" if sets else ""
            return (
                f"{r.get('target_quantity', '?')}x{set_str}"
                f"  max ${r.get('max_price_usd', '?')}"
                f"  {r.get('min_condition', '?')}"
                f"  {r.get('foil', '?')}"
            )

        # Re-read the updated row so we can show the new values
        from manabot.buylist import load_buylist as _load
        try:
            updated_items = [
                i for i in _load(bot.config.buylist_path)
                if i.card_name.lower() == card_name.strip().lower()
            ]
            updated_row = {
                "target_quantity": str(updated_items[0].target_quantity),
                "max_price_usd": str(updated_items[0].max_price_usd),
                "min_condition": updated_items[0].min_condition.value,
                "foil": updated_items[0].foil.value,
                "allowed_sets": ",".join(updated_items[0].allowed_sets),
            } if updated_items else {}
        except Exception:
            updated_row = {}

        msg = f"Updated **{card_name}**:\n  Before: `{_fmt_row(original)}`"
        if updated_row:
            msg += f"\n  After:  `{_fmt_row(updated_row)}`"

        # Ping the original owner if the admin edited someone else's entry
        if force and original:
            other_uids = {
                tag[4:].strip()
                for tag in (original.get("tags") or "").split(",")
                if tag.strip().startswith("uid:") and tag.strip()[4:] != caller_uid
            }
            if other_uids:
                mentions = " ".join(f"<@{u}>" for u in sorted(other_uids))
                msg += f"\n\nFYI {mentions} — your entry was edited by {interaction.user.display_name}."

        await interaction.followup.send(msg)

    return bot


def run_bot(config: Config) -> None:
    """Start the Discord bot. Blocks until the process is killed."""
    if not config.discord_bot_token:
        raise ValueError(
            "discord_bot_token is required. "
            "Set DISCORD_BOT_TOKEN env var or discord.bot_token in config.yaml."
        )
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    bot = create_bot(config)
    bot.run(config.discord_bot_token)
