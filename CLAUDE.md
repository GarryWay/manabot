# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with test deps)
pip install -e ".[test]"

# Run all tests
python -m pytest

# Run a single test file
python -m pytest tests/test_matcher.py -v

# Run a single test by name
python -m pytest tests/test_matcher.py::test_condition_lp_fails_nm_requirement -v

# Run with coverage
python -m pytest --cov=manabot

# CLI entry points
python -m manabot --help
python -m manabot run --buylist data/buylist.csv --dry-run
python -m manabot optimize --buylist data/buylist.csv --dry-run
python -m manabot optimize --over-budget-pct 10 --max-iterations 5
python -m manabot validate-buylist --buylist data/buylist.csv
python -m manabot history --card "Lightning Bolt" --days 30
```

## Architecture

The bot runs a linear pipeline: **Fetch → Match → Analyze → Report**. Each stage is a discrete module; `cli.py:run` is the only place that wires them together.

### Domain model (`manabot/models.py`)

All pipeline stages communicate through four dataclasses:
- `BuyListItem` — one row from the user's CSV
- `PriceListing` — one listing fetched from ManaPool
- `MatchResult` — a `BuyListItem` paired with its filtered `PriceListing` candidates
- `TrendData` — attached to a `MatchResult` by the analyzer

`Condition` supports comparison operators (`>=`, `<`, etc.) via `_CONDITION_RANK`. Always use these operators rather than raw string comparison when checking card condition.

### Pipeline stages

**`manabot/buylist.py`** — Reads the buy list CSV with `csv.DictReader` using `utf-8-sig` encoding (handles Excel BOM). Required columns: `card_name`, `target_quantity`, `max_price_usd`, `min_condition`. Optional: `scryfall_id`, `foil`, `allowed_sets`, `in_universe_only`, `tags`. Extra columns are silently ignored.

**`manabot/api/manapool.py`** — `ManaPoolClient` authenticates with `Email` + `Access-Token` headers. All API response field mapping lives in `_parse_listing()` (JSON) and `_parse_listing_csv()` (bulk export). These are the only methods to update if ManaPool's response schema changes. The API is v0.27.0 and still in active development — verify field names against a live response before assuming they're correct.

**`manabot/matcher.py`** — Five-stage filter pipeline applied to each `BuyListItem`:
1. Match by `scryfall_id` (exact) or normalized card name (no fuzzy matching — ambiguous items become `UNRESOLVED`)
2. Filter by `allowed_sets`
3. Filter by `min_condition` (uses `Condition` comparison operators)
4. Filter by `foil`/`nonfoil`/`any`
5. In-universe filter via Scryfall metadata — degrades to `WARN_SCRYFALL_NEEDED` if `ScryfallClient` is not implemented

**`manabot/analyzer.py`** — Queries `db.get_price_history()` for each matched card and classifies trend as UP/DOWN/FLAT/NEW based on configurable `trend_threshold_pct`. Always runs after `match()` because it needs the DB populated by prior runs.

**`manabot/db.py`** — Thin `sqlite3` wrapper (no ORM). Schema: `price_snapshots` (indexed on `scryfall_id, fetched_at`) and `fetch_runs` (audit log / future scheduling heartbeat). Every `run` always writes snapshots even when no good buys are found — this is intentional to build trend history.

**`manabot/reporter/`** — Four independent reporters all accept `list[MatchResult]`:
- `terminal.py` — Rich table; pass `Console(file=StringIO(), width=200)` in tests to capture output
- `html.py` — Jinja2 template at `manabot/templates/report.html.j2`; self-contained HTML (inline CSS)
- `csv_report.py` — machine-readable summary
- `discord.py` — webhook POST; `dry_run=True` prints payload instead of sending

### Config

`manabot/config.py` loads `config.yaml` first, then overlays environment variables. Required: `MANAPOOL_EMAIL`, `MANAPOOL_TOKEN`. Copy `.env.example` → `.env` to configure.

### Cart optimizer (`manabot/optimizer.py`)

The `optimize` command maximizes **net value** = Σ(max_price_usd × qty) − total_cart_cost, rather than minimizing cost as ManaPool's own optimizer does.

Key design choices:
- **Printing selection**: `build_request_items()` picks the cheapest valid listing's `set_code` to constrain the optimizer to the right printing. In-universe filtering has already happened in the matcher; the optimizer just picks which seller to use.
- **Scoring**: Two-phase. Item-level margins use pre-fetched prices. Cart-level net value uses the optimizer's returned totals (subtotal + shipping + fees).
- **Iteration**: Baseline run first, then one removal trial per negative-margin item. If removing an item improves net value → remove it. If not → lock it (shipping consolidation worth more than the overage). Total API calls ≤ 1 + `max_iterations`.
- **Over-budget threshold**: Items priced above `max_price_usd × (1 + over_budget_pct%)` are excluded before the first optimizer call.
- **Optimizer request format**: `type: "mtg_single"` with `name`, `set_code`, `condition_ids` (all conditions ≥ min_condition), `finish_ids` (`NF`/`FO`/both), `quantity_requested`. No scryfall_id support in the optimizer API.
- `ManaPoolClient.run_optimizer()` streams NDJSON, skips stats lines, returns the last cart object (most optimized).

Config keys: `optimizer_over_budget_pct` (default 0.0), `optimizer_max_iterations` (default 5), `optimizer_destination` (default "US"). All overridable via env vars.

### Not yet implemented

- `manabot/scheduler.py` — raises `NotImplementedError`; wiring point for APScheduler
- Auto-ordering — `POST /buyer/orders/pending-orders` stub noted in `manapool.py`

### Testing

All HTTP calls are mocked with the `responses` library — no real API calls in tests. The `tests/fixtures/` directory contains `sample_buylist.csv` and `sample_prices.json` used across multiple test files. DB tests use SQLite `":memory:"` connections.
