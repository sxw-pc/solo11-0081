"""Synthetic two-warehouse fulfillment intake over generated commerce fixture rows."""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path

def plan_fulfillment(orders, items, stock):
    remaining = {(r["warehouse"], r["product_id"]): int(r["units"]) for r in stock}
    item_by_order = defaultdict(list)
    for item in items:
        item_by_order[item["order_id"]].append(item)
    result = []
    for order in orders:
        allocations = []
        if order["status"] != "paid":
            result.append({"order_id": order["order_id"], "status": "not_paid", "allocations": []})
            continue
        for item in item_by_order[order["order_id"]]:
            needed = int(item["quantity"])
            for warehouse in sorted({r["warehouse"] for r in stock}):
                key = (warehouse, item["product_id"])
                take = min(needed, remaining.get(key, 0))
                if take:
                    remaining[key] -= take
                    needed -= take
                    allocations.append({"item_id": item["item_id"], "warehouse": warehouse, "units": take})
                if needed == 0:
                    break
        result.append({"order_id": order["order_id"], "status": "allocated" if allocations else "backorder",
                       "allocations": allocations})
    return result

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/fulfillment/intake.json"))
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    print(json.dumps(plan_fulfillment(fixture["orders"], fixture["items"], fixture["stock"]), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
