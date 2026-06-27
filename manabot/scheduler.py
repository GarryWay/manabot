"""Scheduler for automated manabot tasks.

Currently implements:
  - Daily seller inventory price update (configurable hour + timezone)

Requires: pip install 'apscheduler>=3.10.4'
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from manabot.config import Config

log = logging.getLogger(__name__)


def schedule_daily_price_update(config: Config) -> None:
    """Start a blocking scheduler that runs the price update daily at the configured local time."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError as e:
        raise ImportError(
            "APScheduler is required for scheduling: pip install 'apscheduler>=3.10.4'"
        ) from e

    from manabot.api.manapool import ManaPoolClient
    from manabot.db import open_db
    from manabot.pricer import PricingConfig, run_pricing_update

    tz = ZoneInfo(config.pricer_schedule_timezone)

    def _price_update_job() -> None:
        log.info("Scheduled price update starting...")
        client = ManaPoolClient(
            email=config.manapool_email,
            token=config.manapool_token,
            use_bulk_export=config.use_bulk_export,
        )
        pricing_cfg = PricingConfig(
            race_to_bottom_threshold=config.pricer_race_to_bottom_threshold,
            min_margin_pct=config.pricer_min_margin_pct,
            cost_floor_days=config.pricer_cost_floor_days,
            iqr_fence_factor=config.pricer_iqr_fence_factor,
            min_sales_for_regression=config.pricer_min_sales_for_regression,
            max_sale_age_days=config.pricer_max_sale_age_days,
            finish_merge_max_price_usd=config.pricer_finish_merge_max_price_usd,
            finish_merge_threshold_usd=config.pricer_finish_merge_threshold_usd,
        )
        try:
            with open_db(config.db_path) as conn:
                run_pricing_update(client, conn, config, pricing_cfg, dry_run=False)
        except Exception:
            log.exception("Price update job failed")

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        _price_update_job,
        "cron",
        hour=config.pricer_schedule_hour,
        minute=0,
        id="daily_price_update",
        misfire_grace_time=3600,  # fire even if service starts up to 1 hour late
    )
    log.info(
        "Price update scheduler started — runs daily at %02d:00 %s",
        config.pricer_schedule_hour,
        config.pricer_schedule_timezone,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")


def schedule_run(cron_expression: str, config: Config) -> None:
    """Legacy stub — use schedule_daily_price_update instead."""
    raise NotImplementedError(
        "schedule_run is not implemented. Use schedule_daily_price_update(config) instead."
    )
