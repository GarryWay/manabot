# manabot

Automated MTG seller tool for [ManaPool](https://manapool.com). Monitors card prices, reprices your inventory against live market data, runs cart optimizations, and surfaces arbitrage opportunities — all from a CLI or Discord bot.

---

## Setup

### Requirements

- Python 3.11+
- A ManaPool seller account with an API access token

### Install

```bash
git clone <repo-url> manabot
cd manabot
python setup_bot.py        # interactive first-time setup (installs deps, configures .env, registers services)
```

Or manually:

```bash
pip install -e ".[full]"   # includes Discord bot + scheduler deps
cp .env.example .env       # then edit .env with your credentials
```

### Configuration

At minimum, `.env` needs:

```
MANAPOOL_EMAIL=you@example.com
MANAPOOL_TOKEN=your-access-token
```

Your ManaPool access token is in your account settings under **API Access**.

For the Discord bot, also add:

```
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_GUILD_ID=your-server-id   # optional: instant slash-command sync
```

### Verify

```bash
python -m manabot --help
python -m manabot price-update --dry-run   # preview repricing without writing
```

---

## CLI Reference

### Inventory repricing

```bash
# Preview what prices would change (no writes)
python -m manabot price-update --dry-run

# Apply live price updates to your ManaPool inventory
python -m manabot price-update

# Show margin report — current prices vs cost basis
python -m manabot margin-report

# Import purchase cost data from a CSV file
python -m manabot import-cost-basis --file purchases.csv

# Start the daily price scheduler (blocks; runs at 2 AM Central by default)
python -m manabot pricer-scheduler
```

### Buylist and cart optimizer

```bash
# Validate your buylist CSV for errors
python -m manabot validate-buylist --buylist data/buylist.csv

# Match your buylist against live ManaPool prices (dry run)
python -m manabot run --buylist data/buylist.csv --dry-run

# Run the cart optimizer (maximizes net value across available sellers)
python -m manabot optimize --buylist data/buylist.csv --dry-run
python -m manabot optimize --over-budget-pct 10 --max-iterations 5

# Price history for a specific card
python -m manabot history --card "Lightning Bolt" --days 30
```

### Arbitrage

```bash
# Find cards priced significantly below TCGPlayer market on ManaPool
python -m manabot arbitrage
python -m manabot arbitrage --min-discount 0.30 --min-price 5.00
```

---

## Discord Bot

### Setup

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
2. Add a bot; under **Privileged Gateway Intents** enable **Message Content Intent**
3. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands` and permissions:
   Send Messages, Attach Files, Embed Links, Use Application Commands
4. Open the generated URL in your browser and add the bot to your server
5. Set `DISCORD_BOT_TOKEN` (and optionally `DISCORD_GUILD_ID`) in `.env`

### Start the bot

```bash
python -m manabot discord-bot
```

Or via `setup_bot.py` — it registers a startup service automatically (systemd on Linux, Task Scheduler on Windows, launchd on macOS).

---

### Slash commands

#### Price & cart commands

| Command | Parameters | Description |
|---------|-----------|-------------|
| `/run` | — | Match your buylist against live ManaPool prices and show results |
| `/optimize` | `over_budget_pct`, `max_cart`, `force_cards`, `arb_riders`, `exclude_preorder` | Run the cart optimizer; returns the best-value seller combination |
| `/arbitrage` | `min_discount_pct`, `min_quantity`, `min_price` | Find ManaPool listings trading below TCGPlayer market value |

#### Buylist management

| Command | Parameters | Description |
|---------|-----------|-------------|
| `/add-card` | `card_name`, `quantity`, `max_price`, `condition`, `set_code`, `foil` | Add a single card — automatically tagged with your username and Discord ID |
| `/add-cards` | `cards` (multi-line) | Add multiple cards at once; one per line in CSV format: `name,qty,price[,condition[,set[,foil]]]` |
| `/buylist` | `tag` (optional) | Show the current buy list; filter by tag to see only your cards (e.g. `user:Garrett`) |
| `/remove-card` | `card_name`, `force` | Remove an entry you added; `force=True` lets you remove anyone's entry |
| `/edit-card` | `card_name`, `quantity`, `max_price`, `condition`, `set_code`, `foil`, `force` | Edit an existing entry — quantity, price, condition, or set restriction |
| `/mark-purchased` | `cards` (optional) | Mark cards as purchased and remove them from the buylist; automatically pings each Discord user whose cards were fulfilled |

---

### How user association works

When you add a card via `/add-card` or `/add-cards`, manabot tags it in the CSV with your Discord display name and user ID:

```
tags: user:Garrett,uid:123456789012345678
```

This powers several features:

- **`/buylist tag:user:Garrett`** — filter the list to only your entries
- **`/remove-card`** — by default you can only remove entries you added; use `force:True` to override
- **`/edit-card`** — same ownership rules; if an admin edits or removes your entry with `force:True`, you get a Discord mention notification
- **`/mark-purchased`** — when a batch of cards is marked purchased, every Discord user who added one of those cards is mentioned in the confirmation message

This means anyone in the server can request cards, and they'll get pinged automatically when their cards are purchased — no manual follow-up needed.

---

## Automated Price Updates

The `pricer-scheduler` command keeps your inventory priced correctly around the clock. It runs once per day at the configured time and reprices every listing based on sales trend, competing ManaPool listings, and TCGPlayer market data.

**Default schedule**: 2:00 AM Central time (handles DST automatically via `America/Chicago` timezone).

To change the schedule, edit `config.yaml`:

```yaml
pricer:
  schedule_hour: 3              # 3 AM
  schedule_timezone: America/New_York
```

Or set environment variables in `.env`:

```
PRICER_SCHEDULE_HOUR=3
PRICER_SCHEDULE_TIMEZONE=America/New_York
```

`setup_bot.py` installs the pricer as a separate persistent service alongside the Discord bot. To upgrade both after pulling new code:

```bash
python setup_bot.py upgrade
```

---

## Pricing Algorithm

Each listing is priced against ManaPool catalog data (sales history + live competing listings) cross-referenced with TCGPlayer market prices via TCGTracking:

1. **Trend projection** — linear regression over recent sales with Tukey IQR outlier removal
2. **Beat the low** — if competing listings exist, price 1¢ below the lowest (race-to-bottom guard: hold at trend if the low is >20% below projection)
3. **TCGPlayer anchor** — when no ManaPool competing listings exist, use TCGPlayer market price directly (more transaction volume than sparse ManaPool data)
4. **Finish pooling** — for cheap cards (<$2) with sparse data, foil and nonfoil sales are pooled together; same logic applied to TCGPlayer finish lookups
5. **Cost floor** — never price below `cost_basis × (1 + min_margin_pct)` within `cost_floor_days` of purchase
6. **Hard floor** — $0.15 minimum regardless of market

---

## Configuration Reference

| Env var | Default | Description |
|---------|---------|-------------|
| `MANAPOOL_EMAIL` | — | **Required** |
| `MANAPOOL_TOKEN` | — | **Required** |
| `DISCORD_BOT_TOKEN` | — | Required for Discord bot |
| `DISCORD_WEBHOOK_URL` | — | Optional webhook for run/optimize report delivery |
| `DISCORD_GUILD_ID` | — | Optional — enables instant slash-command sync for a specific server |
| `PRICER_SCHEDULE_HOUR` | `2` | Local hour for daily auto-reprice (0–23) |
| `PRICER_SCHEDULE_TIMEZONE` | `America/Chicago` | IANA timezone string for the scheduler |
| `PRICER_RACE_TO_BOTTOM_THRESHOLD` | `0.20` | Hold at trend if low price is >20% below projection |
| `PRICER_MIN_MARGIN_PCT` | `0.10` | Minimum margin above cost basis (10%) |
| `PRICER_COST_FLOOR_DAYS` | `30` | Days to enforce cost floor after purchase |
| `DB_PATH` | `data/manabot.db` | SQLite database path |
| `REPORTS_DIR` | `data/reports` | Output directory for HTML/CSV reports |
