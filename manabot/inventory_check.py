"""
Check whether buy list cards are already held in our own ManaPool seller inventory.

Matching mirrors manabot.matcher: scryfall_id when the buy list item has one
pinned, otherwise normalized card-name matching. Condition/finish are ignored —
any copy of the card already in inventory counts as an overlap, since the goal
is to avoid re-buying a card already being sold.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from manabot.matcher import _normalize_name
from manabot.models import BuyListItem, SellerListing


@dataclass
class InventoryOverlap:
    buy_list_item: BuyListItem
    matches: list[SellerListing] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(m.quantity for m in self.matches)


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
