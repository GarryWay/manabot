"""End-to-end integration test for the `run` pipeline."""
import json
from pathlib import Path

import pytest
import responses as resp_mock
from click.testing import CliRunner

from manabot.cli import cli

FIXTURE_PRICES = Path(__file__).parent / "fixtures" / "sample_prices.json"
FIXTURE_BUYLIST = Path(__file__).parent / "fixtures" / "sample_buylist.csv"
BASE = "https://manapool.com/api/v1"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "MANAPOOL_EMAIL": "test@example.com",
        "MANAPOOL_TOKEN": "test-token",
        "DB_PATH": str(tmp_path / "test.db"),
        "REPORTS_DIR": str(tmp_path / "reports"),
        "DISCORD_WEBHOOK_URL": "",
    }


@resp_mock.activate
def test_run_exits_zero(tmp_path):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--no-html"])
    assert result.exit_code == 0, result.output


@resp_mock.activate
def test_run_writes_html_report(tmp_path):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "reports").glob("*.html"))
    assert len(reports) == 1


@resp_mock.activate
def test_run_writes_csv_report(tmp_path):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / "reports").glob("*.csv"))
    assert len(reports) == 1


@resp_mock.activate
def test_run_shows_summary_line(tmp_path):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--no-html"])
    assert "Done." in result.output


@resp_mock.activate
def test_run_dry_run_no_discord(tmp_path):
    resp_mock.add(resp_mock.GET, f"{BASE}/prices/singles", json=json.loads(FIXTURE_PRICES.read_text()))
    runner = CliRunner(env={**_env(tmp_path), "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/fake"})
    result = runner.invoke(cli, ["run", "--buylist", str(FIXTURE_BUYLIST), "--dry-run", "--no-html"])
    assert result.exit_code == 0, result.output
    # dry-run output should contain the word 'dry' somewhere
    assert "dry" in result.output.lower()


def test_validate_buylist_valid(tmp_path):
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["validate-buylist", "--buylist", str(FIXTURE_BUYLIST)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_validate_buylist_missing_file(tmp_path):
    runner = CliRunner(env=_env(tmp_path))
    result = runner.invoke(cli, ["validate-buylist", "--buylist", str(tmp_path / "missing.csv")])
    assert result.exit_code != 0
