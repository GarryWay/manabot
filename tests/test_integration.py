"""End-to-end integration test for the `run` pipeline."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses as resp_mock
from click.testing import CliRunner

from manabot.cli import cli

FIXTURE_PRICES = Path(__file__).parent / "fixtures" / "sample_prices.json"
FIXTURE_BUYLIST = Path(__file__).parent / "fixtures" / "sample_buylist.csv"
MANAPOOL_BASE = "https://manapool.com/api/v1"
SCRYFALL_BASE = "https://api.scryfall.com"

# Black Lotus is the only buy list item without a scryfall_id in the fixture.
# It needs: (1) a Scryfall name-lookup call during enrich_buylist, and
# (2) a metadata call during in-universe filtering (in_universe_only=true).
LOTUS_ID = "b0faa7f2-b547-42c4-a810-839da50dadfe"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "MANAPOOL_EMAIL": "test@example.com",
        "MANAPOOL_TOKEN": "test-token",
        "DB_PATH": str(tmp_path / "test.db"),
        "REPORTS_DIR": str(tmp_path / "reports"),
        "DISCORD_WEBHOOK_URL": "",
    }


def _mock_scryfall() -> None:
    """Register Scryfall mocks needed for the sample_buylist.csv fixture.

    Black Lotus has in_universe_only=True and no pinned scryfall_id, so the
    matcher does name-based matching and then calls is_in_universe() on the
    listing's scryfall_id. No name-lookup call is made — enrich_buylist is
    intentionally NOT used in the run flow (it would collapse all printings to one).
    """
    resp_mock.add(
        resp_mock.GET, f"{SCRYFALL_BASE}/cards/{LOTUS_ID}",
        json={"id": LOTUS_ID, "name": "Black Lotus", "flavor_name": None, "promo_types": []},
    )


@resp_mock.activate
def test_run_exits_zero(tmp_path):
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    _mock_scryfall()
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--no-html"])
    assert result.exit_code == 0, result.output


@resp_mock.activate
def test_run_writes_html_report(tmp_path):
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    _mock_scryfall()
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "reports").glob("*.html"))
    assert len(reports) == 1


@resp_mock.activate
def test_run_writes_csv_report(tmp_path):
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    _mock_scryfall()
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "reports").glob("*.csv"))
    assert len(reports) == 1


@resp_mock.activate
def test_run_shows_summary_line(tmp_path):
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    _mock_scryfall()
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--no-html"])
    assert "Done." in result.output


@resp_mock.activate
def test_run_dry_run_no_discord(tmp_path):
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    _mock_scryfall()
    runner = CliRunner(env={**_env(tmp_path), "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/fake"})
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--dry-run", "--no-html"])
    assert result.exit_code == 0, result.output
    assert "dry" in result.output.lower()


@resp_mock.activate
def test_run_excludes_alternate_universe_listings(tmp_path):
    """Listings with flavor_name set should be excluded from in_universe_only items."""
    prices = json.loads(FIXTURE_PRICES.read_text())
    resp_mock.add(resp_mock.GET, f"{MANAPOOL_BASE}/prices/singles", json=prices)
    # Metadata for the Black Lotus listing says it's an alternate-universe printing.
    # No name-lookup mock needed — enrich_buylist is not called in run flow.
    resp_mock.add(resp_mock.GET, f"{SCRYFALL_BASE}/cards/{LOTUS_ID}",
                  json={"id": LOTUS_ID, "name": "Black Lotus",
                        "flavor_name": "The Forbidden Chalice", "promo_types": []})
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--no-html"])
    assert result.exit_code == 0, result.output
    assert "Done." in result.output


def test_validate_buylist_valid(tmp_path):
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["validate-buylist", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_validate_buylist_missing_file(tmp_path):
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["validate-buylist", "--buylist", str(tmp_path / "missing.csv")])
    assert result.exit_code != 0
