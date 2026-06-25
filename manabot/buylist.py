from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from manabot.models import BuyListItem, Condition, Finish

if TYPE_CHECKING:
    from manabot.api.scryfall import ScryfallClient

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"card_name", "target_quantity", "max_price_usd", "min_condition"}
VALID_CONDITIONS = {c.value for c in Condition}
VALID_FINISHES = {f.value for f in Finish}


def enrich_buylist(items: list[BuyListItem], scryfall_client: "ScryfallClient") -> list[BuyListItem]:
    """Resolve missing scryfall_ids via Scryfall name lookup.

    Mutates items in-place. Items that already have a scryfall_id are skipped.
    Items whose name cannot be resolved keep scryfall_id=None and will fall
    through to name-based matching in the matcher.
    """
    needs_resolution = [item for item in items if item.scryfall_id is None]
    if not needs_resolution:
        return items

    log.info("Resolving scryfall_ids for %d item(s) via Scryfall...", len(needs_resolution))
    for item in needs_resolution:
        scryfall_id = scryfall_client.lookup_by_name(item.card_name)
        if scryfall_id:
            item.scryfall_id = scryfall_id
            log.debug("Resolved %r → %s", item.card_name, scryfall_id)
        else:
            log.warning("Could not resolve Scryfall ID for %r — will use name matching", item.card_name)
    return items


_DEFAULT_FIELDNAMES = [
    "card_name", "scryfall_id", "target_quantity", "max_price_usd",
    "min_condition", "foil", "allowed_sets", "in_universe_only", "tags", "notes",
]


def append_to_buylist(path: Path, item: BuyListItem) -> None:
    """Append one item to the buylist CSV, creating the file (with header) if absent."""
    file_exists = path.exists()

    if file_exists:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or _DEFAULT_FIELDNAMES)
    else:
        fieldnames = _DEFAULT_FIELDNAMES
        path.parent.mkdir(parents=True, exist_ok=True)

    row: dict[str, str] = {fn: "" for fn in fieldnames}
    row.update({
        "card_name": item.card_name,
        "scryfall_id": item.scryfall_id or "",
        "target_quantity": str(item.target_quantity),
        "max_price_usd": str(item.max_price_usd),
        "min_condition": item.min_condition.value,
        "foil": item.foil.value,
        "allowed_sets": ",".join(item.allowed_sets),
        "in_universe_only": "true" if item.in_universe_only else "",
        "tags": ",".join(item.tags),
    })

    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _row_has_uid(row: dict[str, str], uid: str) -> bool:
    """Return True if 'uid:<uid>' appears in the row's tags field."""
    return f"uid:{uid}" in [t.strip() for t in (row.get("tags") or "").split(",")]


def remove_purchases_fifo(
    path: Path,
    purchases: list[tuple[str, int]],
    uid: str | None = None,
) -> list[dict[str, str]]:
    """Remove purchased quantities from the buylist using FIFO order.

    purchases: list of (card_name, qty_to_consume).
        qty == -1 removes every row for that card regardless of quantity.
        Duplicate card names in purchases are accumulated (except -1 wins).

    uid: if given, only rows tagged 'uid:<uid>' are eligible for removal.
        Non-matching rows for the same card name are left in place.

    Returns snapshots of every row that was fully removed or quantity-decremented,
    each augmented with a 'qty_purchased' key (str) showing how many units were
    consumed from that row. Callers can inspect the 'tags' field of returned rows
    to identify which Discord users should be notified.
    """
    if not path.exists():
        return []

    # Build purchase_map: lower_name -> qty_remaining (-1 = remove all)
    purchase_map: dict[str, int] = {}
    for name, qty in purchases:
        lower = name.strip().lower()
        if qty == -1 or purchase_map.get(lower) == -1:
            purchase_map[lower] = -1
        else:
            purchase_map[lower] = purchase_map.get(lower, 0) + qty

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    affected: list[dict[str, str]] = []
    new_rows: list[dict[str, str]] = []

    for row in rows:
        lower_name = row.get("card_name", "").strip().lower()
        if lower_name not in purchase_map:
            new_rows.append(row)
            continue

        # uid filter: skip rows that don't belong to the specified user
        if uid is not None and not _row_has_uid(row, uid):
            new_rows.append(row)
            continue

        qty_remaining = purchase_map[lower_name]
        try:
            row_qty = max(1, int(row.get("target_quantity", "1") or "1"))
        except ValueError:
            row_qty = 1

        if qty_remaining == -1:
            # Remove all copies — don't touch purchase_map so subsequent rows also match
            snapshot = dict(row)
            snapshot["qty_purchased"] = str(row_qty)
            affected.append(snapshot)

        elif qty_remaining >= row_qty:
            # Consume this entire row
            snapshot = dict(row)
            snapshot["qty_purchased"] = str(row_qty)
            affected.append(snapshot)
            purchase_map[lower_name] -= row_qty
            if purchase_map[lower_name] <= 0:
                del purchase_map[lower_name]

        else:
            # Partial purchase — decrement this row's quantity and keep it
            snapshot = dict(row)
            snapshot["qty_purchased"] = str(qty_remaining)
            affected.append(snapshot)
            updated = dict(row)
            updated["target_quantity"] = str(row_qty - qty_remaining)
            new_rows.append(updated)
            del purchase_map[lower_name]

    if affected:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(new_rows)

    return affected


def edit_buylist_entry(
    path: Path,
    card_name: str,
    updates: dict[str, str | None],
    uid: str | None = None,
) -> dict[str, str] | None:
    """Update the oldest (FIFO) matching row in the buylist.

    Matches card_name case-insensitively. If uid is provided, also requires
    'uid:<uid>' in the row's tags field.

    updates: {csv_fieldname: new_value}. None values are skipped (no change).
        Pass "" to explicitly clear a field.

    Returns a copy of the original row before edits, or None if no row matched.
    """
    if not path.exists():
        return None

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    lower_name = card_name.strip().lower()

    for i, row in enumerate(rows):
        if row.get("card_name", "").strip().lower() != lower_name:
            continue
        if uid is not None and not _row_has_uid(row, uid):
            continue

        original = dict(row)
        for field, value in updates.items():
            if value is not None and field in rows[i]:
                rows[i][field] = value

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        return original

    return None


def remove_from_buylist(path: Path, card_names: list[str]) -> int:
    """Remove all rows for the given card names (case-insensitive). Returns row count removed."""
    affected = remove_purchases_fifo(path, [(name, -1) for name in card_names])
    return len(affected)


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

    exclude_ub_raw = (row.get("exclude_ub") or "").lower()
    exclude_ub = exclude_ub_raw in {"true", "1", "yes"}

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
        exclude_ub=exclude_ub,
        tags=tags,
    )
