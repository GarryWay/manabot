from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from manabot.models import BuyListItem, Condition, Finish

REQUIRED_COLUMNS = {"card_name", "target_quantity", "max_price_usd", "min_condition"}
VALID_CONDITIONS = {c.value for c in Condition}
VALID_FINISHES = {f.value for f in Finish}


class BuyListError(ValueError):
    pass


def load_buylist(path: Path) -> list[BuyListItem]:
    """Load and validate a buy list CSV. Raises BuyListError on any bad row."""
    if not path.exists():
        raise FileNotFoundError(f"Buy list not found: {path}")

    items: list[BuyListItem] = []
    errors: list[str] = []

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise BuyListError(f"Buy list is empty: {path}")

        missing = REQUIRED_COLUMNS - {c.strip().lower() for c in reader.fieldnames}
        if missing:
            raise BuyListError(f"Buy list is missing required columns: {', '.join(sorted(missing))}")

        for i, row in enumerate(_normalized_rows(reader), start=2):
            try:
                items.append(_parse_row(row, line=i))
            except BuyListError as e:
                errors.append(str(e))

    if errors:
        raise BuyListError("Buy list has errors:\n" + "\n".join(f"  {e}" for e in errors))

    return items


def _normalized_rows(reader: csv.DictReader) -> Iterator[dict[str, str]]:
    for row in reader:
        yield {k.strip().lower(): (v.strip() if v else "") for k, v in row.items()}


def _parse_row(row: dict[str, str], line: int) -> BuyListItem:
    card_name = row.get("card_name", "")
    if not card_name:
        raise BuyListError(f"Line {line}: card_name is empty")

    try:
        target_quantity = int(row["target_quantity"])
        if target_quantity < 1:
            raise ValueError
    except (ValueError, KeyError):
        raise BuyListError(f"Line {line} ({card_name!r}): target_quantity must be a positive integer")

    try:
        max_price_usd = float(row["max_price_usd"])
        if max_price_usd < 0:
            raise ValueError
    except (ValueError, KeyError):
        raise BuyListError(f"Line {line} ({card_name!r}): max_price_usd must be a non-negative number")

    condition_str = row.get("min_condition", "").upper()
    if condition_str not in VALID_CONDITIONS:
        raise BuyListError(
            f"Line {line} ({card_name!r}): min_condition must be one of {', '.join(sorted(VALID_CONDITIONS))}; got {condition_str!r}"
        )
    min_condition = Condition(condition_str)

    scryfall_id = row.get("scryfall_id") or None

    foil_str = (row.get("foil") or "any").lower()
    if foil_str not in VALID_FINISHES:
        raise BuyListError(
            f"Line {line} ({card_name!r}): foil must be one of {', '.join(sorted(VALID_FINISHES))}; got {foil_str!r}"
        )
    foil = Finish(foil_str)

    allowed_sets_raw = row.get("allowed_sets", "")
    allowed_sets = [s.strip().upper() for s in allowed_sets_raw.split(",") if s.strip()] if allowed_sets_raw else []

    in_universe_raw = (row.get("in_universe_only") or "").lower()
    in_universe_only = in_universe_raw in {"true", "1", "yes"}

    tags_raw = row.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

    return BuyListItem(
        card_name=card_name,
        target_quantity=target_quantity,
        max_price_usd=max_price_usd,
        min_condition=min_condition,
        scryfall_id=scryfall_id,
        foil=foil,
        allowed_sets=allowed_sets,
        in_universe_only=in_universe_only,
        tags=tags,
    )
