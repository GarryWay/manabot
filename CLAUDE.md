# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

Commit directly to `main` — don't create a branch first. The user deploys straight from `main`
themselves; a feature branch just adds a manual merge step they don't want.

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
python -m manabot validate-buylist --buylist data/buylist.csv --fix-names
python -m manabot history --card "Lightning Bolt" --days 30
python -m manabot price-update --dry-run
python -m manabot sell-rules add --set FRA --number 460 --strategy hold --min 280
python -m manabot sell-rules resolve
python -m manabot sell-rules from-inventory --min-price 20
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

**`manabot/buylist.py`** — Reads the buy list CSV with `csv.DictReader` using `utf-8-sig` encoding (handles Excel BOM). Required columns: `card_name`, `target_quantity`, `max_price_usd`, `min_condition`. Optional: `scryfall_id`, `foil`, `allowed_sets`, `in_universe_only`, `tags`. Extra columns are silently ignored. `validate_and_fix_names()` corrects `card_name` typos against Scryfall: pinned rows (`scryfall_id` set) are corrected to that id's own name since matcher stage 1 already matches by id and the text is cosmetic; unpinned rows are corrected via exact/fuzzy name lookup (`ScryfallClient.resolve_canonical_name()`) without writing a `scryfall_id`, so name-based matching still considers every printing rather than getting silently pinned. Runs nightly via `scheduler.py`, or on demand via `validate-buylist --fix-names`.

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
- **Optimizer request format**: `type: "mtg_single"` with `name`, `card_id`, `set_code` (only when `allowed_sets` set), `condition_ids` (all conditions ≥ min_condition), `finish_ids` (`NF`/`FO`/both), `quantity_requested`. As of ~2026-08 the API requires one of `card_id`/`mtgjson_id`/`set_code`+`collector_number` per item (`name` alone stopped being enough — confirmed via `git log` that `_build_optimizer_payload()` hadn't changed since 2026-06-25, and confirmed live by resending a bare-`name` payload from a local `.env`-configured client on 2026-08-16, which still 400s identically — this is ManaPool-side, not a manabot regression). `card_id` is resolved via `ManaPoolClient.get_card_ids_by_scryfall_id()` — `GET /products/singles?scryfall_ids=...`, batched at 100/call, keyed by the pre-fetched listing's `scryfall_id`. This is ManaPool's own catalog identifier (an mtgjson-style UUID per their docs), sourced directly from their API rather than derived/guessed — two earlier guesses (scryfall_id itself, then a Scryfall oracle_id) were both tried and disproven via live 400/409 responses before landing on this.
  - **`card_id` does NOT pin to one printing** — verified live: requesting 600× Lightning Bolt with `card_id` from one specific printing (528 in stock) still fulfilled all 600, sourced across 18 different sets. This matches the API docs literally: "any interchangeable printing of it may be substituted." The `scryfall_id` used to resolve `card_id` only determines *which card* gets requested, not which printing gets bought — "Any printings" search is fully preserved, unlike `mtgjson_id` (documented separately as the field that pins one specific printing).
  - **Pinned rows (`scryfall_id` set) send `set_code` + `collector_number` instead of `card_id`** — the printing's own set/number from the same `/products/singles` lookup (`ManaPoolClient.get_singles_by_scryfall_id()`). Verified live 2026-10-02: 50× M10 #146 Lightning Bolt came back entirely from M10 #146 listings, and 600× (373 in stock) returned 409 rather than substituting. `/products/singles` does not return `mtgjson_id`, so that pair is the available printing pin. This applies to forced cards too (force is just a margin bypass keyed on `card_name`).
  - `optimizer._resolve_card_ids()` does the resolution inside `find_best_cart()` (skips items that already carry a `card_id`, e.g. from arbitrage candidates); items with no resolvable card_id are dropped with a warning rather than sent unidentified, since one bad item 400s the whole request.
- `ManaPoolClient.run_optimizer()` streams NDJSON, skips stats lines, returns the last cart object (most optimized).

Config keys: `optimizer_over_budget_pct` (default 0.0), `optimizer_max_iterations` (default 5), `optimizer_destination` (default "US"). All overridable via env vars.

### Seller pricer (`manabot/pricer.py`, `manabot/sell_rules.py`)

`compute_price()` decides each listing's price; the module docstring has the full algorithm. In order: a ManaPool signal (beat the low, or hold at the sales-regression projection when the low is a race to the bottom), then a **TCGPlayer cap**, then the cost floor and $0.15 hard floor, then the listing's **sell rule**.
- **TCGPlayer cap**: applies when the price is ≥ `pricer_cross_market_min_usd` ($20) or ManaPool has ≤ `pricer_cross_market_max_mp_qty` (4) competing copies. It caps the price at TCG low − $0.01, or at TCG market when TCG low is a race to the bottom (same `_is_race_to_bottom` guard). It only ever lowers the price, and only for English listings. Data comes from TCGTracking (`get_sku()` matches the SKU's `lng`; each product has one SKU per condition/finish/language).
- The ManaPool catalog's `low_price` / `available_quantity` **exclude our own listings** (verified 2026-10-02: our $300 listing sat under a catalog low of $429), so `available_quantity` is competitor stock as-is.
- **Sell rules**: one CSV row per printing (`data/sell_rules.csv`, gitignored like the buy list; see `sell_rules.csv.example`). Rows are matched by `mtgjson_id` (unique per double-sided token pairing) or else `scryfall_id`, with optional condition/finish; the most specific row wins. `aggressive` skips both race-to-bottom guards and always applies the TCG cap; `hold` keeps the current price. `min`/`max` override the cost floor; only the hard floor beats them. `unclamped_price_usd` keeps what the strategy would have set (for `hold`, the balanced price), and `bounds_report()` flags listings whose unclamped price is more than `pricer_bounds_report_pct` (15%) outside min/max. The scheduler writes those to `reports_dir/sell_rules_review_<date>.csv` and posts them to the Discord webhook; `price-update` prints them and writes the CSV.
- `sell-rules add|resolve|from-inventory` fill in the ids from set code + collector number. Our own seller inventory is checked first, because it carries ManaPool's `mtgjson_id` and handles compound DFT numbers like `2-7`; otherwise Scryfall's `/cards/:set/:number` lookup is used.

### Scheduler (`manabot/scheduler.py`)

`schedule_daily_price_update()` is the real, implemented scheduler (requires `apscheduler`) — one daily APScheduler cron job (configurable hour + timezone) that runs, in order: seller inventory price update (`pricer.py`, plus the sell-rules bounds report), buy list name validation (`validate_and_fix_names()`), then buy list coalesce (`coalesce_buylist()` — merges duplicate rows). Name validation runs before coalesce so a correction that makes two rows identical gets merged the same night. `schedule_run()` is an unrelated legacy stub that still raises `NotImplementedError` — don't confuse the two.

### Not yet implemented

- Auto-ordering — `POST /buyer/orders/pending-orders` stub noted in `manapool.py`

### Testing

All HTTP calls are mocked with the `responses` library — no real API calls in tests. The `tests/fixtures/` directory contains `sample_buylist.csv` and `sample_prices.json` used across multiple test files. DB tests use SQLite `":memory:"` connections.
