from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from manabot.models import MatchResult

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def write(results: list[MatchResult], reports_dir: Path, run_at: datetime, summary: dict) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"report_{run_at.strftime('%Y%m%d_%H%M%S')}.html"

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html.j2")
    html = template.render(results=results, summary=summary, run_at=run_at)
    path.write_text(html, encoding="utf-8")
    return path
