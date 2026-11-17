"""Fictional jurisdiction tax preview for synthetic commerce rows."""
from __future__ import annotations
import argparse
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

def preview_tax(orders, items, rates):
    by_order = {o["order_id"]: o for o in orders}
    result = []
    for item in items:
        order = by_order[item["order_id"]]
        rate = Decimal(str(rates[order["destination"]][item["tax_category"]]))
        amount = Decimal(str(item["line_total"]))
        tax = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        result.append({"order_id": order["order_id"], "item_id": item["item_id"],
                       "destination": order["destination"], "tax": str(tax)})
    return result

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/tax/intake.json"))
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    print(json.dumps(preview_tax(data["orders"], data["items"], data["rates"]), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
