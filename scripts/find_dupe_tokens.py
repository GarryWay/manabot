"""Identify double-sided token listings that have a single-sided face also listed.

Run from the repo root:
    python scripts/find_dupe_tokens.py

Reads .env / config.yaml for credentials — no catalog download needed.
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

from manabot.api.manapool import ManaPoolClient
from manabot.config import load_config

config = load_config()
client = ManaPoolClient(email=config.manapool_email, token=config.manapool_token)

print("Fetching seller inventory...")
inventory = client.get_seller_inventory()
print(f"  {len(inventory)} listing(s) found\n")

# Index all listings by (set_code, card_name) → list of listings
by_name: dict[tuple[str, str], list] = defaultdict(list)
for listing in inventory:
    by_name[(listing.set_code, listing.card_name)].append(listing)

dupes: list[dict] = []

for listing in inventory:
    if "//" not in listing.card_name:
        continue
    faces = [f.strip() for f in listing.card_name.split("//")]
    for face in faces:
        matches = by_name.get((listing.set_code, face), [])
        for match in matches:
            # Same finish and condition only — apples to apples
            if match.finish == listing.finish and match.condition == listing.condition:
                dupes.append({
                    "set": listing.set_code,
                    "dft_name": listing.card_name,
                    "dft_qty": listing.quantity,
                    "dft_price": listing.price_usd,
                    "dft_inventory_id": listing.inventory_id,
                    "face_name": face,
                    "face_qty": match.quantity,
                    "face_price": match.price_usd,
                    "face_inventory_id": match.inventory_id,
                    "condition": listing.condition.value,
                    "finish": listing.finish.value,
                })

if not dupes:
    print("No duplicates found — inventory looks clean.")
    sys.exit(0)

# Unique (set, single-sided name) pairs
seen: set[tuple[str, str]] = set()
print(f"Single-sided listings to check ({len({(d['set'], d['face_name']) for d in dupes})} unique):\n")
for d in sorted(dupes, key=lambda x: (x["face_name"], x["set"])):
    key = (d["set"], d["face_name"])
    if key not in seen:
        seen.add(key)
        print(f"  [{d['set']}]  {d['face_name']}")
