"""Per-card selling rules for the pricer, read from a buy-list-style CSV.

Each row targets one printing of a card we sell and sets a pricing strategy plus
optional price bounds:

  aggressive — always undercut the lowest listing (skip the race-to-bottom guard on
               both ManaPool and TCGPlayer) and always compare against TCGPlayer
  balanced   — the default pricer behaviour
  hold       — keep the current listing price; only min/max move it

min_price_usd / max_price_usd clamp the final price (they override the cost floor).
When the strategy's own price falls well outside those bounds, the pricer flags the
listing for manual review instead of silently pinning it (see pricer.bounds_report_rows).

Columns: card_name, set_code, collector_number, scryfall_id, mtgjson_id, condition,
finish, strategy, min_price_usd, max_price_usd, notes. A row needs scryfall_id (or
mtgjson_id) to match anything; `sell-rules resolve` fills those from set_code +
collector_number. condition and finish are optional — blank matches any, and a row
that names them wins over one that doesn't. Extra columns are ignored.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from manabot.models import Condition, Finish

if TYPE_CHECKING:
    from manabot.api.scryfall import ScryfallClient
    from manabot.models import SellerListing

log = logging.getLogger(__name__)

STRATEGIES = ("aggressive", "balanced", "hold")

COLUMNS = [
    "card_name", "set_code", "collector_number", "scryfall_id", "mtgjson_id",
    "condition", "finish", "strategy", "min_price_usd", "max_price_usd", "notes",
]


@dataclass
class SellRule:
    card_name: str = ""
    set_code: str = ""
    collector_number: str = ""
    scryfall_id: str = ""
    mtgjson_id: str = ""
    condition: Optional[Condition] = None   # None = any condition
    finish: Optional[Finish] = None         # None = any finish
    strategy: str = "balanced"
    min_price_usd: Optional[float] = None
    max_price_usd: Optional[float] = None
    notes: str = ""

    def matches(self, listing: "SellerListing") -> bool:
        # mtgjson_id is the only id unique per double-sided token pairing (scryfall_id
        # is just the front face), so it takes precedence when the rule carries one.
        if self.mtgjson_id:
            if self.mtgjson_id != listing.mtgjson_id:
                return False
        elif not self.scryfall_id or self.scryfall_id != listing.scryfall_id:
            return False
        if self.condition is not None and self.condition != listing.condition:
            return False
        if self.finish is not None and self.finish != listing.finish:
            return False
        return True

    @property
    def specificity(self) -> int:
        return (self.condition is not None) + (self.finish is not None) + bool(self.mtgjson_id)


def _parse_price(raw: str) -> Optional[float]:
    raw = (raw or "").strip().lstrip("$")
    return float(raw) if raw else None


def _parse_row(row: dict, line_no: int) -> Optional[SellRule]:
    get = lambda k: (row.get(k) or "").strip()  # noqa: E731
    strategy = get("strategy").lower() or "balanced"
    if strategy not in STRATEGIES:
        log.warning("sell rules line %d: unknown strategy %r — skipping row", line_no, strategy)
        return None
    try:
        condition = Condition(get("condition").upper()) if get("condition") else None
        finish = Finish(get("finish").lower()) if get("finish") else None
        min_price = _parse_price(get("min_price_usd"))
        max_price = _parse_price(get("max_price_usd"))
    except ValueError as e:
        log.warning("sell rules line %d: %s — skipping row", line_no, e)
        return None
    if min_price is not None and max_price is not None and min_price > max_price:
        log.warning("sell rules line %d: min_price_usd > max_price_usd — skipping row", line_no)
        return None
    return SellRule(
        card_name=get("card_name"),
        set_code=get("set_code").upper(),
        collector_number=get("collector_number"),
        scryfall_id=get("scryfall_id"),
        mtgjson_id=get("mtgjson_id"),
        condition=condition,
        finish=finish,
        strategy=strategy,
        min_price_usd=min_price,
        max_price_usd=max_price,
        notes=get("notes"),
    )


def load_sell_rules(path: Path) -> list[SellRule]:
    """Read the sell rules CSV. A missing file means no rules."""
    path = Path(path)
    if not path.exists():
        return []
    rules: list[SellRule] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            rule = _parse_row(row, line_no)
            if rule is None:
                continue
            if not (rule.scryfall_id or rule.mtgjson_id):
                log.warning(
                    "sell rules line %d (%s %s #%s): no scryfall_id — run `sell-rules resolve`; skipping",
                    line_no, rule.card_name, rule.set_code, rule.collector_number,
                )
                continue
            rules.append(rule)
    return rules


def find_rule(rules: Iterable[SellRule], listing: "SellerListing") -> Optional[SellRule]:
    """Most specific matching rule for a listing, or None."""
    matching = [r for r in rules if r.matches(listing)]
    return max(matching, key=lambda r: r.specificity) if matching else None


# ---------------------------------------------------------------------------
# CSV editing helpers (used by the `sell-rules` CLI)
# ---------------------------------------------------------------------------

def read_rows(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = [k for r in rows for k in r if k not in COLUMNS]
    fieldnames = COLUMNS + list(dict.fromkeys(extra))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") or "" for k in fieldnames})


def resolve_row(
    row: dict,
    inventory: list["SellerListing"],
    scryfall: Optional["ScryfallClient"] = None,
) -> str:
    """Fill a row's ids and card_name in place from set_code + collector_number.

    Our own ManaPool inventory is checked first: a listing there gives the exact
    scryfall_id and mtgjson_id ManaPool uses (the only way to pin a double-sided token
    pairing, whose number is compound like "2-7"). Otherwise Scryfall's set/number
    lookup supplies scryfall_id and the canonical name.

    Returns "resolved", "already" (ids were present), or "unresolved".
    """
    if (row.get("scryfall_id") or "").strip() or (row.get("mtgjson_id") or "").strip():
        return "already"
    set_code = (row.get("set_code") or "").strip().upper()
    number = (row.get("collector_number") or "").strip()
    if not set_code or not number:
        return "unresolved"

    for listing in inventory:
        if listing.set_code.upper() == set_code and listing.number == number:
            row["scryfall_id"] = listing.scryfall_id
            row["mtgjson_id"] = listing.mtgjson_id
            row["card_name"] = listing.card_name
            row["set_code"] = set_code
            return "resolved"

    if scryfall is None or "-" in number:
        return "unresolved"
    scryfall_id = scryfall.lookup_by_set_number(set_code, number)
    if not scryfall_id:
        return "unresolved"
    row["scryfall_id"] = scryfall_id
    row["set_code"] = set_code
    name = scryfall.get_card_metadata(scryfall_id).get("name")
    if name:
        given = (row.get("card_name") or "").strip()
        if given and given.casefold() != name.casefold():
            log.warning("set %s #%s is %r, not %r — using Scryfall's name", set_code, number, name, given)
        row["card_name"] = name
    return "resolved"


def row_from_listing(listing: "SellerListing", strategy: str = "balanced") -> dict:
    return {
        "card_name": listing.card_name,
        "set_code": listing.set_code,
        "collector_number": listing.number,
        "scryfall_id": listing.scryfall_id,
        "mtgjson_id": listing.mtgjson_id,
        "condition": listing.condition.value,
        "finish": listing.finish.value,
        "strategy": strategy,
    }
