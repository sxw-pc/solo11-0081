"""Offline diagnostic for cross-table commerce consistency in generated fixtures."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .engine import generate_dataset
from .money import allocate_by_weight, format_cents, split_units, to_cents
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


def audit_settlement_snapshot(fixture: dict[str, Any]) -> dict[str, Any]:
    """Independently audit the original-sale snapshot behind settlements.

    Every check runs on the immutable sale rows (gross, line-level and
    order-level discounts, per-line tax, captured instruments) before the
    ledger is allowed to emit a refund instruction.  Returning structured
    errors instead of raising here keeps the audit report itself honest;
    the CLI/ledger treats a non-coherent result as a hard abort.
    """
    errors: list[str] = []
    orders = {row["order_id"]: row for row in fixture.get("orders", [])}
    lines_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("items", []):
        lines_by_order[row["order_id"]].append(row)
    payments_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("payments", []):
        payments_by_order[row["order_id"]].append(row)

    audit_rows = 0
    for order_id, order in orders.items():
        rows = sorted(lines_by_order.get(order_id, []), key=lambda r: r["item_id"])
        if not rows:
            errors.append(f"order {order_id} has no item lines")
            continue
        gross_values: list[int] = []
        net_values: list[int] = []
        tax_values: list[int] = []
        valid_rows: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        for row in rows:
            audit_rows += 1
            if row["item_id"] in seen_items:
                errors.append(f"order {order_id} repeats item {row['item_id']}")
            seen_items.add(row["item_id"])
            try:
                quantity = int(row["quantity"])
            except (TypeError, ValueError):
                errors.append(f"line {row['item_id']} has non-integer quantity")
                continue
            if quantity <= 0:
                errors.append(f"line {row['item_id']} has non-positive quantity")
                continue
            try:
                unit_gross = to_cents(row["unit_gross"])
                gross = to_cents(row["gross_amount"])
                line_discount = to_cents(row["line_discount"])
                net = to_cents(row["line_total"])
                tax = to_cents(row["tax_amount"])
            except (TypeError, ValueError) as exc:
                errors.append(f"line {row['item_id']} invalid money: {exc}")
                continue
            if gross != unit_gross * quantity:
                errors.append(
                    f"line {row['item_id']} gross {format_cents(gross)} != "
                    f"unit gross x {quantity}")
            if min(gross, line_discount, net, tax) < 0:
                errors.append(f"line {row['item_id']} has a negative component")
            if sum(split_units(net, quantity)) != net:  # whole-cent split sanity
                errors.append(f"line {row['item_id']} net cannot be unit-allocated")
            gross_values.append(gross)
            net_values.append(net)
            tax_values.append(tax)
            valid_rows.append(row)

        order_gross = sum(gross_values)
        promo = order.get("promotion")
        if promo is None:
            threshold, promo_discount = 0, 0
        else:
            try:
                threshold = to_cents(promo["threshold"])
                promo_discount = to_cents(promo["discount"])
            except (TypeError, ValueError) as exc:
                errors.append(f"order {order_id} invalid promotion money: {exc}")
                threshold, promo_discount = 0, 0
        if promo is not None and order_gross < threshold and promo_discount:
            errors.append(
                f"order {order_id} grants a promotion below its own threshold")
        if promo_discount > order_gross - sum(net_values):
            errors.append(
                f"order {order_id} promotion exceeds its pre-promo margin")
        if not valid_rows:
            errors.append(f"order {order_id} has no valid item lines")
            continue
        shares = allocate_by_weight(gross_values, promo_discount)
        for row, share, net in zip(valid_rows, shares, net_values):
            expected_net = (
                to_cents(row["gross_amount"])
                - to_cents(row["line_discount"]) - share
            )
            if expected_net != net:
                errors.append(
                    f"line {row['item_id']} line_total conflicts with the "
                    "gross-weighted order promotion share")

        payments = payments_by_order.get(order_id, [])
        if not payments:
            errors.append(f"order {order_id} has no captured payment instruments")
        captured = 0
        seen_payments: set[str] = set()
        for payment in payments:
            if payment["payment_id"] in seen_payments:
                errors.append(f"order {order_id} repeats payment "
                              f"{payment['payment_id']}")
            seen_payments.add(payment["payment_id"])
            try:
                amount = to_cents(payment["captured_amount"])
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"payment {payment['payment_id']} invalid money: {exc}")
                continue
            if amount <= 0:
                errors.append(f"payment {payment['payment_id']} not captured")
            captured += amount
        expected_captured = sum(net_values) + sum(tax_values)
        if captured != expected_captured:
            errors.append(
                f"order {order_id} captured {format_cents(captured)} != net "
                f"{format_cents(sum(net_values))} + tax "
                f"{format_cents(sum(tax_values))}")

    for row in fixture.get("items", []):
        if row["order_id"] not in orders:
            errors.append(f"line {row['item_id']} references unknown order "
                          f"{row['order_id']}")
    for row in fixture.get("payments", []):
        if row["order_id"] not in orders:
            errors.append(f"payment {row['payment_id']} references unknown order "
                          f"{row['order_id']}")
    return {
        "status": "coherent" if not errors else "inconsistent",
        "errors": errors,
        "counts": {"orders": len(orders), "order_items": audit_rows,
                   "payments": len(fixture.get("payments", []))},
    }


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
