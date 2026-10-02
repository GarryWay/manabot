"""Tests for TCGTrackingClient SKU lookup."""
from __future__ import annotations

from manabot.api.tcgtracking import TCGSKUPricing, TCGTrackingClient


def _client(tmp_path, skus: list[TCGSKUPricing]) -> TCGTrackingClient:
    client = TCGTrackingClient(cache_dir=tmp_path)
    client._loaded_sets.add("TST")
    client._set_index["TST"] = 1
    client._product_index["sf-1"] = 100
    client._sku_index[100] = skus
    return client


def _sku(language: str, low: float) -> TCGSKUPricing:
    return TCGSKUPricing(condition="NM", finish="Foil", low=low, market=low, high=low,
                         listing_count=1, mp_price=None, language=language)


def test_get_sku_matches_language_not_just_first_condition_finish(tmp_path):
    """Each product carries one SKU per language; a French SKU listed first must not
    be returned for an English lookup."""
    client = _client(tmp_path, [_sku("FR", 5.0), _sku("EN", 250.0)])
    assert client.get_sku("sf-1", "TST", "NM", "foil").low == 250.0
    assert client.get_sku("sf-1", "TST", "NM", "foil", language="FR").low == 5.0
    assert client.get_sku("sf-1", "TST", "NM", "foil", language="JA") is None
