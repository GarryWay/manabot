import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    manapool_email: str
    manapool_token: str
    discord_webhook_url: str = ""
    discord_bot_token: str = ""
    discord_guild_id: int | None = None  # sync slash commands to this guild (instant) vs globally (up to 1h)
    buylist_path: Path = Path("data/buylist.csv")
    db_path: Path = Path("data/manabot.db")
    reports_dir: Path = Path("data/reports")
    use_bulk_export: bool = False
    max_price_age_days: int = 1
    trend_window_days: int = 7
    trend_threshold_pct: float = 5.0
    optimizer_over_budget_pct: float = 0.0   # allow items up to this % above max_price
    optimizer_max_cart_usd: float | None = None  # hard spending cap per run (None = no limit)
    optimizer_max_iterations: int = 5        # max removal trials per optimize run
    optimizer_destination: str = "US"        # shipping destination country code
    arbitrage_min_market_price_usd: float = 2.00  # NM floor below this = skip card entirely
    # Required for pending order creation/purchase: name, line1, city, state (2-char), postal_code, country
    shipping_address: dict | None = None
    billing_address: dict | None = None  # defaults to shipping_address if not set
    # schedule_cron: str | None = None  # future: APScheduler cron expression


def load_config(config_path: Path | None = None) -> Config:
    """Load config from config.yaml, then overlay environment variables."""
    base: dict = {}

    path = config_path or Path("config.yaml")
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        mp = raw.get("manapool", {})
        base["manapool_email"] = mp.get("email", "")
        base["manapool_token"] = mp.get("token", "")
        discord_cfg = raw.get("discord", {})
        base["discord_webhook_url"] = discord_cfg.get("webhook_url", "")
        if "bot_token" in discord_cfg:
            base["discord_bot_token"] = discord_cfg["bot_token"]
        if "guild_id" in discord_cfg:
            base["discord_guild_id"] = int(discord_cfg["guild_id"])
        paths = raw.get("paths", {})
        if "buylist" in paths:
            base["buylist_path"] = Path(paths["buylist"])
        if "db" in paths:
            base["db_path"] = Path(paths["db"])
        if "reports_dir" in paths:
            base["reports_dir"] = Path(paths["reports_dir"])
        behavior = raw.get("behavior", {})
        if "use_bulk_export" in behavior:
            base["use_bulk_export"] = bool(behavior["use_bulk_export"])
        if "max_price_age_days" in behavior:
            base["max_price_age_days"] = int(behavior["max_price_age_days"])
        if "trend_window_days" in behavior:
            base["trend_window_days"] = int(behavior["trend_window_days"])
        if "trend_threshold_pct" in behavior:
            base["trend_threshold_pct"] = float(behavior["trend_threshold_pct"])
        optimizer = raw.get("optimizer", {})
        if "over_budget_pct" in optimizer:
            base["optimizer_over_budget_pct"] = float(optimizer["over_budget_pct"])
        if "max_cart_usd" in optimizer:
            base["optimizer_max_cart_usd"] = float(optimizer["max_cart_usd"])
        if "max_iterations" in optimizer:
            base["optimizer_max_iterations"] = int(optimizer["max_iterations"])
        if "destination" in optimizer:
            base["optimizer_destination"] = str(optimizer["destination"])
        if "min_market_price_usd" in optimizer:
            base["arbitrage_min_market_price_usd"] = float(optimizer["min_market_price_usd"])
        if "shipping_address" in optimizer:
            addr = {k: str(v) if k == "postal_code" else v
                    for k, v in optimizer["shipping_address"].items()}
            base["shipping_address"] = addr
        if "billing_address" in optimizer:
            addr = {k: str(v) if k == "postal_code" else v
                    for k, v in optimizer["billing_address"].items()}
            base["billing_address"] = addr

    # Environment variables override config.yaml
    if os.getenv("MANAPOOL_EMAIL"):
        base["manapool_email"] = os.environ["MANAPOOL_EMAIL"]
    if os.getenv("MANAPOOL_TOKEN"):
        base["manapool_token"] = os.environ["MANAPOOL_TOKEN"]
    if os.getenv("DISCORD_WEBHOOK_URL"):
        base["discord_webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
    if os.getenv("DISCORD_BOT_TOKEN"):
        base["discord_bot_token"] = os.environ["DISCORD_BOT_TOKEN"]
    if os.getenv("DISCORD_GUILD_ID"):
        base["discord_guild_id"] = int(os.environ["DISCORD_GUILD_ID"])
    if os.getenv("BUYLIST_PATH"):
        base["buylist_path"] = Path(os.environ["BUYLIST_PATH"])
    if os.getenv("DB_PATH"):
        base["db_path"] = Path(os.environ["DB_PATH"])
    if os.getenv("REPORTS_DIR"):
        base["reports_dir"] = Path(os.environ["REPORTS_DIR"])
    if os.getenv("USE_BULK_EXPORT"):
        base["use_bulk_export"] = os.environ["USE_BULK_EXPORT"].lower() == "true"
    if os.getenv("MAX_PRICE_AGE_DAYS"):
        base["max_price_age_days"] = int(os.environ["MAX_PRICE_AGE_DAYS"])
    if os.getenv("TREND_WINDOW_DAYS"):
        base["trend_window_days"] = int(os.environ["TREND_WINDOW_DAYS"])
    if os.getenv("TREND_THRESHOLD_PCT"):
        base["trend_threshold_pct"] = float(os.environ["TREND_THRESHOLD_PCT"])
    if os.getenv("OPTIMIZER_OVER_BUDGET_PCT"):
        base["optimizer_over_budget_pct"] = float(os.environ["OPTIMIZER_OVER_BUDGET_PCT"])
    if os.getenv("OPTIMIZER_MAX_CART_USD"):
        base["optimizer_max_cart_usd"] = float(os.environ["OPTIMIZER_MAX_CART_USD"])
    if os.getenv("OPTIMIZER_MAX_ITERATIONS"):
        base["optimizer_max_iterations"] = int(os.environ["OPTIMIZER_MAX_ITERATIONS"])
    if os.getenv("OPTIMIZER_DESTINATION"):
        base["optimizer_destination"] = os.environ["OPTIMIZER_DESTINATION"]

    email = base.get("manapool_email", "")

    token = base.get("manapool_token", "")
    if not email or not token:
        raise ValueError(
            "MANAPOOL_EMAIL and MANAPOOL_TOKEN are required. "
            "Set them in .env or config.yaml."
        )

    return Config(
        manapool_email=email,
        manapool_token=token,
        discord_webhook_url=base.get("discord_webhook_url", ""),
        discord_bot_token=base.get("discord_bot_token", ""),
        discord_guild_id=base.get("discord_guild_id", None),
        buylist_path=base.get("buylist_path", Path("data/buylist.csv")),
        db_path=base.get("db_path", Path("data/manabot.db")),
        reports_dir=base.get("reports_dir", Path("data/reports")),
        use_bulk_export=base.get("use_bulk_export", False),
        max_price_age_days=base.get("max_price_age_days", 1),
        trend_window_days=base.get("trend_window_days", 7),
        trend_threshold_pct=base.get("trend_threshold_pct", 5.0),
        optimizer_over_budget_pct=base.get("optimizer_over_budget_pct", 0.0),
        optimizer_max_cart_usd=base.get("optimizer_max_cart_usd", None),
        optimizer_max_iterations=base.get("optimizer_max_iterations", 5),
        optimizer_destination=base.get("optimizer_destination", "US"),
        arbitrage_min_market_price_usd=base.get("arbitrage_min_market_price_usd", 2.00),
        shipping_address=base.get("shipping_address", None),
        billing_address=base.get("billing_address", None),
    )
