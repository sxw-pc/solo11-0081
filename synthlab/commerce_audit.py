"""Offline diagnostic for cross-table commerce consistency in generated fixtures."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .engine import generate_dataset
from .spec.loader import load_dataset
from .spec.model import DatasetData


def _rows(data: DatasetData, name: str) -> list[dict[str, Any]]:
    table = data.tables[name]
    return [dict(zip(table.columns, row)) for row in table.rows]


def audit_commerce_rows(
    products: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report mismatches without editing any generated values."""
    products_by_id = {row["product_id"]: row for row in products}
    orders_by_id = {row["order_id"]: row for row in orders}
    totals: dict[str, Decimal] = defaultdict(Decimal)
    quantities: dict[str, int] = defaultdict(int)
    stock_use: dict[str, int] = defaultdict(int)
    counts = {name: 0 for name in (
        "line_amount", "product_price", "order_total", "item_count", "stock_overuse",
    )}
    for row in items:
        product = products_by_id.get(row["product_id"])
        order = orders_by_id.get(row["order_id"])
        if product is None or order is None:
            raise ValueError("commerce audit requires valid order/product references")
        quantity = int(row["quantity"])
        price = Decimal(str(row["unit_price"]))
        discount = Decimal(str(row["discount_pct"]))
        expected = (quantity * price * (Decimal("1") - discount)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        if Decimal(str(row["line_total"])) != expected:
            counts["line_amount"] += 1
        if price != Decimal(str(product["unit_price"])):
            counts["product_price"] += 1
        totals[row["order_id"]] += Decimal(str(row["line_total"]))
        quantities[row["order_id"]] += quantity
        if order["status"] in ("paid", "shipped"):
            stock_use[row["product_id"]] += quantity
    for order_id, order in orders_by_id.items():
        if Decimal(str(order["total_amount"])) != totals[order_id]:
            counts["order_total"] += 1
        if int(order["item_count"]) != quantities[order_id]:
            counts["item_count"] += 1
    for product_id, used in stock_use.items():
        if used > int(products_by_id[product_id]["stock_quantity"]):
            counts["stock_overuse"] += 1
    return {
        "status": "coherent" if not any(counts.values()) else "inconsistent",
        "counts": counts,
        "rows": {"products": len(products), "orders": len(orders), "order_items": len(items)},
    }


def audit_generated_commerce(data: DatasetData) -> dict[str, Any]:
    return audit_commerce_rows(_rows(data, "products"), _rows(data, "orders"), _rows(data, "order_items"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check generated order accounting and stock")
    parser.add_argument("--spec", type=Path, default=Path("fixtures/demo/dataset.yaml"))
    args = parser.parse_args(argv)
    data = generate_dataset(load_dataset(args.spec))
    result = audit_generated_commerce(data)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "coherent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
