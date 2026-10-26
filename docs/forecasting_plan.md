# Price forecasting plan

Goal: forecast each tracked card's future value from its own history and from how
similar cards have moved, and feed those forecasts into selling (pricer) and buying
(optimizer, arbitrage). We forecast the *structural* lifecycle (release spike, the
dip as product floods the market, the recovery once sealed product stops being
opened, reprints, Standard rotation), **not** meta-driven spikes. Human outlook
overrides cover what the model can't see.

## Data

Neither source keeps history for us, so we archive it ourselves, nightly, from
`pricer-scheduler` (`manabot/forecast/history.py`, CLI `history-ingest`). It goes into
its own SQLite file `data/price_history.db` (`history_db_path`):

| Table | Source | Contents |
|---|---|---|
| `printings` | Scryfall `default_cards` | static metadata: oracle_id, set, set_type, rarity, release date, treatment, reprint, reserved |
| `scryfall_daily` | Scryfall `default_cards` | usd/usd_foil/usd_etched (TCGPlayer market), eur, tix, edhrec_rank |
| `manapool_daily` | ManaPool catalog | market, lowest ask (any / NM / LP+ × finish), available quantity (supply) |
| `manapool_sales` | ManaPool catalog `recent_sales` | every sale seen, deduplicated |

Findings that shaped this (2026-10-26):
- The old `price_snapshots` table holds only 3 runs (Jun–Jul 2026), which isn't usable as history.
- ManaPool's catalog `recent_sales` is capped at the **20 most recent sales per
  variant**. The first ingest backfilled 1.76M sales (back to 2023 for illiquid
  cards, but only weeks back for liquid ones). Archived daily, it becomes a complete
  sales log from now on.
- ManaPool's `price_market` is null for many high-end printings, so Scryfall's
  TCG market price is archived alongside it.
- MTGStocks has multi-year history, but its API costs about $1k/yr, which isn't worth it.
- Printings priced below `history.min_price_usd` ($0.50) in every finish are
  skipped. Growth is about 4 MB/day of daily rows plus sales, roughly 1.8 GB/yr.

## Tracked universe

The universe is recomputed weekly (`forecast/universe.py`):
- **Tier A (per-card forecast, ~1–3k printings):** market ≥ ~$5 and liquid (sales
  per 30 days from `manapool_sales`, plus ManaPool supply). Anything in our inventory, on the
  buy list, or carrying an override is always included.
- **Tier B (everything archived):** contributes to cohort curves only.
- Thin markets (fewer than ~3 sales/month) get a neutral forecast.

## Features

- **Lifecycle:** days since set release, set_type, rarity, treatment
  (borderless/showcase/serialized/foil/etched).
- **Supply:** reprint count, days since the last printing of the oracle card,
  number of interchangeable printings, ManaPool available quantity and its trend.
- **Long-run demand (not meta):** edhrec_rank, breadth of format legality, reserved list.
- **Calendar:** known Standard rotation dates.
- **Card state:** price level, 30/90-day momentum, volatility, and how far the card sits from its cohort curve.

## Models

Each model is adopted only if it beats the previous one in walk-forward backtest.

- **M0, no change.** This is the bar to beat. Every report states whether we did.
- **M1, cohort lifecycle curve.** A log-price index by (set_type × rarity × treatment)
  against set age, fitted across all archived sets. Because every set is at a
  different age on any given day, even a few months of archive covers years of
  lifecycle cross-sectionally. Forecast = price × curve(age+h) / curve(age).
- **M2, M1 plus card-level drift** partially pooled toward the cohort, plus
  reprint and rotation regressors.
- **M3, gradient boosting** on the full feature set. It gets considered once we have ~6+ months of archive.

Every model outputs quantiles (p10/p50/p90) of the log return at h = 30/60/90/180/365
days. Training uses winsorized returns and a robust loss. A spike detector (e.g. >30%
in <14 days with no lifecycle explanation) marks a card `confidence=low` and
neutral. We neither chase the spike nor bet on its reversal; that's what overrides are for.

## Backtesting (`forecast backtest`)

- Walk-forward from rolling origins with strictly point-in-time features. Reprint
  counts, for example, are as of the forecast date.
- Forecast metrics: MAE of log return vs. M0, directional hit rate on moves over 10%,
  and p10–p90 coverage. Reported with and without spike-flagged cards.
- **Decision backtest:** replay the hold/sell and buy decisions below against realized
  `manapool_sales`, with fees, and compare realized NPV against the current pricer. A model
  ships only if it improves decisions, not just accuracy.

## Hold vs. sell: NPV against a risk-free rate

Holding costs are negligible, so the only alternative to holding a card is holding cash.

```
sell_now  = P0 × (1 − fee)
hold(h)   = Q_h × (1 − fee) / (1 + r)^(h/365)       for h in forecast horizons
reservation_price = max_h hold(h) / (1 − fee)
```

- `r` = `forecast_risk_free_rate` (config, e.g. the current T-bill yield).
- `Q_h` = the forecast price at horizon h, at quantile `forecast_hold_quantile`
  (0.5 = risk-neutral; lower = demand more confidence before holding).
- **Pricer:** list at `max(normal pricer price, reservation_price)`, capped at
  `forecast_max_hold_premium_pct` above market. A sale happens only if a buyer pays
  more than holding is worth. When the reservation price is below market (the forecast is
  falling faster than r), the listing switches to the existing `aggressive` behaviour.
  The reasons `forecast_hold` / `forecast_sell` are logged to `price_updates`. Sell
  rules still win, and the hard floor still applies.
- **Arbitrage:** a candidate's value becomes `max_h hold(h)` rather than the current market
  floor. We buy if that beats cost by the margin.
- **Buy list / optimizer:** `max_price_usd` stays the user's number, and forecasts never raise it.
  Items forecast to fall faster than `r` over their horizon are flagged "wait" in
  `/optimize` output, and are deferred from the cart only if `forecast_defer_buys` is on.

## Human overrides (`data/forecast_overrides.csv`, gitignored)

```
scope,id,outlook,target_usd,horizon_days,expires,note
oracle,<oracle_id>,bullish,45,90,2027-02-01,"commander demand, reprint unlikely"
printing,<scryfall_id>,sell,,,2026-12-15,"short-term spike, move it"
```

- `scope`: `oracle` (all printings) or `printing`.
- `outlook`:
  - `bullish`/`bearish`: `target_usd` at `horizon_days` replaces the model's p50, or
    shifts it ±20% without a target.
  - `hold`: the reservation price is at least `target_usd`.
  - `sell`: list aggressively.
  - `neutral`: ignore the model.
- `expires` is required. The nightly report lists overrides that are about to expire.
- CLI: `forecast override add|list|expire`.
- This file is separate from `sell_rules.csv`. Sell rules are pricing mechanics for one
  printing; outlook is a view on the card and drives buying too.

## Upkeep

- **Nightly (`pricer-scheduler`):** archive, compute forecasts for Tier A, and score matured
  forecasts against what actually happened.
- **Weekly:** refresh the universe, refit, and run a mini-backtest. Each forecast row records its `model_version`.
- **Kill switch:** if live error trails M0 for N consecutive weeks, integration
  reverts to neutral and posts to the Discord webhook. Integration is off by default
  (`forecast_enabled`), with a flag per consumer.
- Model dependencies (pandas/numpy/lightgbm) go in an optional `[forecast]` extra.

## Rollout

| Phase | Scope | Gate | Status |
|---|---|---|---|
| 0 | Nightly archive (Scryfall + ManaPool catalog + sales) | none | **done** |
| 1 | Universe, features, M0/M1, backtest harness | backtest report | after ~4–8 weeks of archive |
| 2 | Overrides file + shadow mode (forecasts and would-be decisions logged only) | ~4–6 weeks shadow | |
| 3 | Pricer NPV reservation price, tight caps | positive decision backtest | |
| 4 | Arbitrage valuation + optimizer buy-timing flags | Phase 3 stable | |
| 5 | M2/M3 | beats M1 walk-forward | ~6+ months of archive |
