from __future__ import annotations

from typing import Optional


class ScryfallClient:
    """Scryfall API client — stub until Phase 3+ implementation."""

    def lookup_by_name(self, name: str) -> Optional[str]:
        """Return scryfall_id for a card name, or None if not found."""
        raise NotImplementedError(
            "Scryfall name lookup is not yet implemented. "
            "Add a scryfall_id to your buy list entry to skip this lookup."
        )

    def get_card_metadata(self, scryfall_id: str) -> dict:
        """Return card metadata including set_type, promo, games, finishes."""
        raise NotImplementedError(
            "Scryfall metadata lookup is not yet implemented. "
            "in_universe_only filtering requires this — remove that flag or implement this method."
        )
