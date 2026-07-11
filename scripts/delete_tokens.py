"""Delete token listings from seller inventory.

Usage:
    # Dry run — show all token listings that would be deleted
    python scripts/delete_tokens.py

    # Delete all token listings (set codes starting with T)
    python scripts/delete_tokens.py --delete
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

parser = argparse.ArgumentParser()
parser.add_argument("--delete", action="store_true", help="Delete all identified token listings")
args = parser.parse_args()

from manabot.api.manapool import ManaPoolClient, ManaPoolAPIError
from manabot.config import load_config

config = load_config()
client = ManaPoolClient(email=config.manapool_email, token=config.manapool_token)

# ── Identify all token listings ─────────────────────────────────────────────

print("Fetching seller inventory...")
inventory = client.get_seller_inventory()
print(f"  {len(inventory)} listing(s) found\n")

tokens = [l for l in inventory if l.set_code.startswith("T")]

if not tokens:
    print("No token listings found.")
    sys.exit(0)

print(f"Token listings to delete ({len(tokens)}):\n")
print(f"  {'Set':<6}  {'Cond':<4}  {'Finish':<8}  {'Qty':>3}  {'Price':>7}  {'Name'}")
print("  " + "-" * 80)
for l in sorted(tokens, key=lambda x: (x.set_code, x.card_name)):
    print(
        f"  {l.set_code:<6}  {l.condition.value:<4}  {l.finish.value:<8}  "
        f"{l.quantity:>3}x  ${l.price_usd:>6.2f}  {l.card_name}"
    )

if not args.delete:
    print("\nDry run — pass --delete to actually remove these listings.")
    sys.exit(0)

# ── Confirm and delete ───────────────────────────────────────────────────────

answer = input(f"\nDelete all {len(tokens)} token listing(s)? [y/N] ").strip().lower()
if answer != "y":
    print("Aborted.")
    sys.exit(0)

ok = 0
fail = 0
for l in tokens:
    try:
        client.delete_seller_listing(l)
        print(f"  Deleted  {l.set_code}  {l.condition.value}  {l.finish.value}  {l.card_name}")
        ok += 1
    except ManaPoolAPIError as e:
        print(f"  FAILED   {l.set_code}  {l.card_name}: {e}")
        fail += 1

print(f"\nDone: {ok} deleted, {fail} failed.")
