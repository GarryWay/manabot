"""
Check whether buy list cards are already held in our own ManaPool seller inventory.

Matching mirrors manabot.matcher: scryfall_id when the buy list item has one
pinned, otherwise normalized card-name matching. Condition/finish are ignored —
any copy of the card already in inventory counts as an overlap, since the goal
is to avoid re-buying a card already being sold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from manabot.matcher import _normalize_name
from manabot.models import BuyListItem, SellerListing

if TYPE_CHECKING:
    from pathlib import Path

    from manabot.api.manapool import ManaPoolClient


@dataclass
class InventoryOverlap:
    buy_list_item: BuyListItem
    matches: list[SellerListing] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(m.quantity for m in self.matches)

    @property
    def overlap_quantity(self) -> int:
        """The amount actually shared between the buy list want and inventory stock —
        never more than either side, so it's the safe amount to remove from both."""
        return min(self.buy_list_item.target_quantity, self.total_quantity)


def find_overlap(
    buy_list: list[BuyListItem],
    seller_inventory: list[SellerListing],
) -> list[InventoryOverlap]:
    """Return one InventoryOverlap per buy list item that has matching stock in inventory.

    Items with no matching inventory are omitted from the result.
    """
    by_scryfall_id: dict[str, list[SellerListing]] = {}
    by_name: dict[str, list[SellerListing]] = {}
    for listing in seller_inventory:
        if listing.scryfall_id:
            by_scryfall_id.setdefault(listing.scryfall_id, []).append(listing)
        by_name.setdefault(_normalize_name(listing.card_name), []).append(listing)

    overlaps: list[InventoryOverlap] = []
    for item in buy_list:
        if item.scryfall_id:
            matches = by_scryfall_id.get(item.scryfall_id, [])
        else:
            matches = by_name.get(_normalize_name(item.card_name), [])
        if matches:
            overlaps.append(InventoryOverlap(buy_list_item=item, matches=matches))
    return overlaps


@dataclass
class ForceRemoveResult:
    listings_deleted: int = 0
    listings_updated: int = 0
    inventory_qty_removed: int = 0
    buylist_decremented: list[dict] = field(default_factory=list)  # rows from remove_purchases_fifo
    removal_errors: list[str] = field(default_factory=list)


def apply_force_remove(
    overlaps: list[InventoryOverlap],
    client: "ManaPoolClient",
    buylist_path: "Path",
) -> ForceRemoveResult:
    """Remove exactly the overlapping quantity from both inventory and the buy list.

    For each overlap, only `overlap_quantity` units are pulled from inventory
    (deleting listings that are fully consumed, shrinking the quantity on
    listings that are only partially consumed) — any inventory surplus beyond
    what the buy list wanted is left listed. The buy list is decremented by
    however much was *actually* removed from inventory (not the intended
    amount), so a failed API call doesn't drop a buy list entry for stock
    that's still listed.
    """
    from manabot.buylist import remove_purchases_fifo

    result = ForceRemoveResult()
    purchases: list[tuple[str, int]] = []

    for o in overlaps:
        remaining = o.overlap_quantity
        if remaining <= 0:
            continue
        actually_removed = 0
        for m in o.matches:
            if remaining <= 0:
                break
            take = min(remaining, m.quantity)
            try:
                if take >= m.quantity:
                    client.delete_seller_listing(m)
                    result.listings_deleted += 1
                else:
                    client.update_seller_listing_price(m, m.price_usd, m.quantity - take)
                    result.listings_updated += 1
            except Exception as e:
                result.removal_errors.append(f"{m.card_name} [{m.set_code}]: {e}")
                continue
            result.inventory_qty_removed += take
            actually_removed += take
            remaining -= take

        if actually_removed > 0:
            purchases.append((o.buy_list_item.card_name, actually_removed))

    if purchases:
        result.buylist_decremented = remove_purchases_fifo(buylist_path, purchases)

    return result
