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
pip install -e ".[full]"     # includes scheduler + Discord bot deps
# or for a minimal install:
pip install -e .
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```
MANAPOOL_EMAIL=you@example.com
MANAPOOL_TOKEN=your-access-token
```

Your ManaPool access token is in your account settings under **API Access**.

### Verify

```bash
python -m manabot --help
python -m manabot price-update --dry-run   # preview prices without updating
```

---

## CLI Reference

### Price management

```bash
# Preview what prices would change (no writes)
python -m manabot price-update --dry-run

# Apply live price updates to your ManaPool inventory
python -m manabot price-update

# Show margin report (cost basis vs current prices)
python -m manabot margin-report

# Import purchase cost data from a CSV
python -m manabot import-cost-basis --file purchases.csv

# Start the daily scheduler (runs at 2 AM Central by default)
python -m manabot pricer-scheduler
```

### Buylist / optimizer

```bash
# Validate your buylist CSV
python -m manabot validate-buylist --buylist data/buylist.csv

# Run the buylist matcher (find cards available on ManaPool)
python -m manabot run --buylist data/buylist.csv --dry-run

# Run the cart optimizer (maximizes net value across available sellers)
python -m manabot optimize --buylist data/buylist.csv --dry-run
python -m manabot optimize --over-budget-pct 10 --max-iterations 5

# Price history for a card
python -m manabot history --card "Lightning Bolt" --days 30
```

### Arbitrage

```bash
# Find cards priced significantly below TCGPlayer market
python -m manabot arbitrage
python -m manabot arbitrage --min-discount 0.30 --min-price 5.00
```

---

## Discord Bot

### Setup

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
2. Add a bot, enable **Message Content Intent** and **Server Members Intent**
3. Copy the bot token into `.env`:
   ```
   DISCORD_BOT_TOKEN=your-bot-token
   DISCORD_GUILD_ID=your-server-id   # optional: faster slash-command sync
   ```
4. Invite the bot to your server with the `bot` + `applications.commands` scopes

### Start the bot

```bash
python -m manabot discord-bot
```

### Slash commands

| Command | Description |
|---------|-------------|
| `/run` | Run the buylist matcher and show results |
| `/optimize` | Run the cart optimizer |
| `/arbitrage` | Show arbitrage opportunities |
| `/price-update` | Reprice your inventory (dry-run by default) |
| `/price-update live:True` | Apply live price updates |
| `/history card:<name>` | Show price history for a card |
| `/margin-report` | Show inventory margin summary |

All commands support a `dry_run` parameter (default `True`) so you can preview before committing changes.

---

## Automated Price Updates

The `pricer-scheduler` command starts a long-running process that reprices your inventory once per day at the configured time.

**Default**: 2:00 AM Central time (handles DST automatically).

To change the schedule, set in `.env` or `config.yaml`:

```
PRICER_SCHEDULE_HOUR=3          # 3 AM
PRICER_SCHEDULE_TIMEZONE=America/New_York
```

On a Linux server, run the scheduler as a systemd service — see [DEPLOY.md](DEPLOY.md) for step-by-step instructions.

---

## Pricing Algorithm

Each listing is priced against ManaPool catalog data (sales history + live competing listings) and cross-referenced with TCGPlayer market prices via TCGTracking:

1. **Trend projection** — linear regression over recent sales, with Tukey IQR outlier removal
2. **Beat the low** — if competing listings exist, price 1¢ below the lowest (unless doing so would be a race-to-bottom vs the trend)
3. **TCGPlayer anchor** — when no ManaPool listings exist, use TCGPlayer market price directly
4. **Cost floor** — never price below `cost_basis × (1 + min_margin_pct)` within `cost_floor_days` of purchase
5. **Hard floor** — $0.15 minimum regardless of market

---

## Configuration Reference

All settings can be placed in `config.yaml` or overridden with environment variables.

| Env var | Default | Description |
|---------|---------|-------------|
| `MANAPOOL_EMAIL` | — | **Required** |
| `MANAPOOL_TOKEN` | — | **Required** |
| `DISCORD_BOT_TOKEN` | — | Required for Discord bot |
| `DISCORD_WEBHOOK_URL` | — | Optional webhook for reports |
| `PRICER_SCHEDULE_HOUR` | `2` | Local hour for daily auto-update |
| `PRICER_SCHEDULE_TIMEZONE` | `America/Chicago` | IANA timezone for scheduler |
| `PRICER_RACE_TO_BOTTOM_THRESHOLD` | `0.20` | Hold at trend if low is >20% below |
| `PRICER_MIN_MARGIN_PCT` | `0.10` | Minimum margin above cost basis |
| `PRICER_COST_FLOOR_DAYS` | `30` | Days to enforce cost floor after purchase |
| `DB_PATH` | `data/manabot.db` | SQLite database path |
| `REPORTS_DIR` | `data/reports` | Output directory for HTML/CSV reports |
