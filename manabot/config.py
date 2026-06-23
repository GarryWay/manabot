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
    buylist_path: Path = Path("data/buylist.csv")
    db_path: Path = Path("data/manabot.db")
    reports_dir: Path = Path("data/reports")
    use_bulk_export: bool = False
    max_price_age_days: int = 1
    trend_window_days: int = 7
    trend_threshold_pct: float = 5.0
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
        base["discord_webhook_url"] = raw.get("discord", {}).get("webhook_url", "")
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

    # Environment variables override config.yaml
    if os.getenv("MANAPOOL_EMAIL"):
        base["manapool_email"] = os.environ["MANAPOOL_EMAIL"]
    if os.getenv("MANAPOOL_TOKEN"):
        base["manapool_token"] = os.environ["MANAPOOL_TOKEN"]
    if os.getenv("DISCORD_WEBHOOK_URL"):
        base["discord_webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
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
        buylist_path=base.get("buylist_path", Path("data/buylist.csv")),
        db_path=base.get("db_path", Path("data/manabot.db")),
        reports_dir=base.get("reports_dir", Path("data/reports")),
        use_bulk_export=base.get("use_bulk_export", False),
        max_price_age_days=base.get("max_price_age_days", 1),
        trend_window_days=base.get("trend_window_days", 7),
        trend_threshold_pct=base.get("trend_threshold_pct", 5.0),
    )
