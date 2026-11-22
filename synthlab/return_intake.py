"""Offline return intake over synthetic commerce fixture rows.

Legacy fixture contracts (``orders`` / ``items`` / ``claims`` with
``line_total`` only) keep the original per-line ``refund_cap`` output.
Settlement fixtures additionally carry the original payment / promotion /
tax snapshot and are routed through :mod:`synthlab.settlement`, which
turns eligible requests into cash, store-credit or rejection decisions.
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .commerce_audit import audit_settlement_snapshot
from .settlement import SettlementError, SettlementLedger, settle_fixture


def evaluate_claim(order, item, claim, used):
    """Shared quantity/status eligibility rule.

    Returns ``None`` when the claim is eligible, otherwise the rejection
    reason code.  Both the legacy cap-based intake and the downstream
    settlement ledger use this, so an intake eligibility change always
    changes the settleable unit set.
    """
    if order is None or item is None or item["order_id"] != claim["order_id"]:
        return "unknown_line"
    if order["status"] not in ("paid", "shipped"):
        return "unpaid_order"
    quantity = int(claim["quantity"])
    if quantity <= 0 or used[claim["item_id"]] + quantity > int(item["quantity"]):
        return "quantity_exceeded"
    return None


def assess_returns(orders, items, claims):
    order_by_id = {r["order_id"]: r for r in orders}
    item_by_id = {r["item_id"]: r for r in items}
    used = defaultdict(int)
    decisions = []
    for claim in claims:
        item = item_by_id.get(claim["item_id"])
        order = order_by_id.get(claim["order_id"])
        quantity = int(claim["quantity"])
        code = evaluate_claim(order, item, claim, used)
        if code is None:
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


def _is_settlement_fixture(fixture):
    return "requests" in fixture or "payments" in fixture


def _write_ledger(path: Path, ledger: SettlementLedger) -> None:
    path.write_text(
        json.dumps(ledger.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def run_settlement(fixture, ledger_path: Path | None = None):
    """Audit the original-sale snapshot, then settle every request.

    The snapshot audit runs first and a failure aborts before any refund
    instruction is produced, so a broken upstream row cannot be hidden by
    a pretty end report.
    """
    audit = audit_settlement_snapshot(fixture)
    if audit["status"] != "coherent":
        raise SettlementError(
            "settlement aborted by original-sale audit: "
            + json.dumps(audit["errors"], ensure_ascii=False, sort_keys=True))
    ledger = SettlementLedger.from_fixture(fixture)
    result = settle_fixture(fixture, ledger=ledger)
    if ledger_path is not None:
        _write_ledger(ledger_path, ledger)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path,
                        default=Path("fixtures/returns/intake.json"))
    parser.add_argument("--ledger-out", type=Path, default=None,
                        help="write the serialized settlement ledger as JSON")
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if _is_settlement_fixture(fixture):
        result = run_settlement(fixture, ledger_path=args.ledger_out)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    decisions = assess_returns(fixture["orders"], fixture["items"], fixture["claims"])
    print(json.dumps(decisions, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
