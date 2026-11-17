"""Offline return intake over synthetic commerce fixture rows."""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

def assess_returns(orders, items, claims):
    order_by_id = {r["order_id"]: r for r in orders}
    item_by_id = {r["item_id"]: r for r in items}
    used = defaultdict(int)
    decisions = []
    for claim in claims:
        item = item_by_id.get(claim["item_id"])
        order = order_by_id.get(claim["order_id"])
        quantity = int(claim["quantity"])
        code = None
        if order is None or item is None or item["order_id"] != claim["order_id"]:
            code = "unknown_line"
        elif order["status"] not in ("paid", "shipped"):
            code = "unpaid_order"
        elif quantity <= 0 or used[claim["item_id"]] + quantity > int(item["quantity"]):
            code = "quantity_exceeded"
        else:
            used[claim["item_id"]] += quantity
        decisions.append({
            "claim_id": claim["claim_id"],
            "status": "accepted" if code is None else "rejected",
            "reason": code,
            "refund_cap": str(
                (Decimal(str(item["line_total"])) * quantity /
                 Decimal(str(item["quantity"]))).quantize(Decimal("0.01"))
            ) if code is None else None,
        })
    return decisions

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path,
                        default=Path("fixtures/returns/intake.json"))
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    print(json.dumps(assess_returns(fixture["orders"], fixture["items"], fixture["claims"]),
                     sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
