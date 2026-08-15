from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Condition(str, Enum):
    NM = "NM"   # Near Mint
    LP = "LP"   # Lightly Played
    MP = "MP"   # Moderately Played
    HP = "HP"   # Heavily Played
    DMG = "DMG" # Damaged

    def __ge__(self, other: "Condition") -> bool:
        return _CONDITION_RANK[self] >= _CONDITION_RANK[other]

    def __gt__(self, other: "Condition") -> bool:
        return _CONDITION_RANK[self] > _CONDITION_RANK[other]

    def __le__(self, other: "Condition") -> bool:
        return _CONDITION_RANK[self] <= _CONDITION_RANK[other]

    def __lt__(self, other: "Condition") -> bool:
        return _CONDITION_RANK[self] < _CONDITION_RANK[other]


# Higher rank = better condition
_CONDITION_RANK: dict[Condition, int] = {
    Condition.NM: 5,
    Condition.LP: 4,
    Condition.MP: 3,
    Condition.HP: 2,
    Condition.DMG: 1,
}


class Finish(str, Enum):
    NONFOIL = "nonfoil"
    FOIL = "foil"
    ANY = "any"


class TrendDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    NEW = "NEW"  # no history available


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    UNRESOLVED = "UNRESOLVED"           # no listing found after all filters
    WARN_SCRYFALL_NEEDED = "WARN_SCRYFALL_NEEDED"  # in_universe_only requested but Scryfall unavailable


@dataclass
class BuyListItem:
    card_name: str
    target_quantity: int
    max_price_usd: float
    min_condition: Condition
    scryfall_id: Optional[str] = None
    foil: Finish = Finish.ANY
    allowed_sets: list[str] = field(default_factory=list)
    in_universe_only: bool = False
    exclude_ub: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class PriceListing:
    scryfall_id: str
    card_name: str
    set_code: str
    condition: Condition
    finish: Finish
    price_usd: float
    quantity_available: int
    seller_id: str
    fetched_at: datetime
    market_price_usd: Optional[float] = None  # price_market from ManaPool (None if not available)
    url: str = ""  # ManaPool card page URL


@dataclass
class TrendData:
    scryfall_id: str
    price_now: float
    price_then: Optional[float]  # None when no history
    direction: TrendDirection

    @property
    def change_pct(self) -> Optional[float]:
        if self.price_then is None or self.price_then == 0:
            return None
        return ((self.price_now - self.price_then) / self.price_then) * 100


@dataclass
class MatchResult:
    buy_list_item: BuyListItem
    listings: list[PriceListing] = field(default_factory=list)
    best_price: Optional[float] = None
    is_good_buy: bool = False
    trend: Optional[TrendData] = None
    status: MatchStatus = MatchStatus.UNRESOLVED


@dataclass
class CartRequestItem:
    """A single item prepared for the ManaPool optimizer request."""
    buy_list_item: BuyListItem
    set_code: str           # constrains optimizer to this printing (from cheapest valid listing)
    estimated_price: float  # best price from pre-fetched data
    estimated_margin: float  # max_price - estimated_price (positive = under budget)
    condition_ids: list[str]  # e.g. ["NM", "LP"] for min_condition=LP
    finish_ids: list[str]     # e.g. ["NF"] for nonfoil
    seller_id: str = ""     # seller from the pre-fetch scan (proxy for optimizer's choice)


@dataclass
class CartResult:
    """Result from a single ManaPool optimizer run, scored against buy list values."""
    items: list[CartRequestItem]
    raw_cart: list[dict]     # [{inventory_id, quantity_selected}] from optimizer
    subtotal_usd: float
    shipping_usd: float
    fees_usd: float
    total_usd: float
    value_budget_usd: float  # Σ(max_price_i × qty_i) for items in this cart
    net_value_usd: float     # value_budget - total_usd

    @property
    def is_profitable(self) -> bool:
        return self.net_value_usd > 0


@dataclass
class SellerListing:
    """Our own inventory listing on ManaPool (seller side)."""
    inventory_id: str       # UUID from GET /seller/inventory
    product_id: str         # ManaPool product UUID — required for DFT updates
    scryfall_id: str
    card_name: str
    set_code: str
    condition: Condition
    finish: Finish
    language: str           # "EN", "JA", etc.
    quantity: int
    price_usd: float


@dataclass
class CompletedSale:
    """A completed seller order item from GET /seller/orders."""
    order_id: str
    scryfall_id: str
    card_name: str
    set_code: str
    condition: Condition
    finish: Finish
    quantity: int
    sold_price_usd: float
    sold_at: datetime
