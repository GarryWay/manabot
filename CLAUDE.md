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

### Not yet implemented

- `manabot/api/scryfall.py` — both methods raise `NotImplementedError`; needed for `in_universe_only` filtering and name-based scryfall_id resolution
- `manabot/scheduler.py` — raises `NotImplementedError`; wiring point for APScheduler
- Auto-ordering — `POST /buyer/orders/pending-orders` stub noted in `manapool.py`

### Testing

All HTTP calls are mocked with the `responses` library — no real API calls in tests. The `tests/fixtures/` directory contains `sample_buylist.csv` and `sample_prices.json` used across multiple test files. DB tests use SQLite `":memory:"` connections.
