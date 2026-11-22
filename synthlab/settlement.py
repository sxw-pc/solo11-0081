"""Downstream economic settlement of return requests.

The intake layer (``return_intake.assess_returns``) only decides quantity
eligibility and a per-line cap.  This module turns *eligible* returns into
payable settlements on top of the immutable original sale snapshot:

* exact integer-cent arithmetic (see :mod:`synthlab.money`), never float;
* stable remainder allocation by unit index, so any legal return order
  converges on the same cumulative totals;
* two independently authorized promotion contracts
  (``retain_promotion`` / ``reprice_kept``), fixed per order at first
  settlement and never switchable afterwards;
* cash refunds spread across the still-open balances of the original
  capture instruments (all-or-nothing), or store-credit liabilities that
  block the same return from being paid in cash later;
* request-id idempotency with payload-conflict rejection, and revocations
  that recompute the cumulative payable from the current settled set.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .money import allocate_by_weight, format_cents, split_units, to_cents

RETAIN_PROMOTION = "retain_promotion"
REPRICE_KEPT = "reprice_kept"
CONTRACTS = (RETAIN_PROMOTION, REPRICE_KEPT)
CASH = "cash"
STORE_CREDIT = "store_credit"
ROUTES = (CASH, STORE_CREDIT)


class SettlementError(ValueError):
    """A settlement snapshot or request violates the accounting contract."""


class SettlementInvariantError(RuntimeError):
    """Internal cumulative totals broke an invariant that must always hold."""


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _positive_int(value: Any, what: str) -> int:
    """Coerce a quantity field, rejecting floats and malformed strings."""
    if isinstance(value, bool) or isinstance(value, float):
        raise SettlementError(f"{what} must be a whole integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SettlementError(f"{what} must be a whole integer") from exc


def _safe_int(value: Any) -> int:
    """Best-effort integer coercion for rejected-request bookkeeping."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LineSnapshot:
    item_id: str
    order_id: str
    product_id: str
    quantity: int
    gross: int
    line_discount: int
    promo_share: int
    net: int
    tax: int
    unit_gross: list[int]
    unit_line_discount: list[int]
    unit_net: list[int]
    unit_tax: list[int]


@dataclass(frozen=True)
class PaymentSnapshot:
    payment_id: str
    order_id: str
    captured: int


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    status: str
    threshold: int
    promo_discount: int
    default_contract: Optional[str]
    lines: tuple[LineSnapshot, ...]
    payments: tuple[PaymentSnapshot, ...]
    gross: int
    total_line_discount: int
    net_total: int
    tax_total: int
    captured_total: int

    def line(self, item_id: str) -> Optional[LineSnapshot]:
        for line in self.lines:
            if line.item_id == item_id:
                return line
        return None


def _require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def build_snapshots(fixture: dict[str, Any]) -> dict[str, OrderSnapshot]:
    """Parse a settlement fixture into validated immutable order snapshots."""
    errors: list[str] = []
    raw_orders = {row["order_id"]: row for row in fixture.get("orders", [])}
    raw_lines: dict[str, list[dict[str, Any]]] = {}
    for row in fixture.get("items", []):
        raw_lines.setdefault(row["order_id"], []).append(row)
    raw_payments: dict[str, list[dict[str, Any]]] = {}
    for row in fixture.get("payments", []):
        raw_payments.setdefault(row["order_id"], []).append(row)

    snapshots: dict[str, OrderSnapshot] = {}
    for order_id, order in raw_orders.items():
        lines_raw = sorted(raw_lines.get(order_id, []), key=lambda r: r["item_id"])
        _require(bool(lines_raw), errors, f"order {order_id} has no item lines")
        if not lines_raw:
            continue

        gross_values, line_discount_values, net_values, tax_values = [], [], [], []
        parsed_lines: list[LineSnapshot] = []
        seen_items: set[str] = set()
        for row in lines_raw:
            item_id = row["item_id"]
            _require(item_id not in seen_items, errors,
                     f"duplicate item_id {item_id} in order {order_id}")
            seen_items.add(item_id)
            quantity = _positive_int(row["quantity"], f"line {item_id} quantity")
            _require(quantity > 0, errors,
                     f"line {item_id} quantity must be positive")
            try:
                unit_gross = to_cents(row["unit_gross"])
                gross_amount = to_cents(row["gross_amount"])
                line_discount = to_cents(row["line_discount"])
                net = to_cents(row["line_total"])
                tax = to_cents(row["tax_amount"])
            except (TypeError, ValueError) as exc:
                errors.append(f"line {item_id} invalid money: {exc}")
                continue
            gross = unit_gross * quantity
            _require(gross_amount == gross, errors,
                     f"line {item_id} gross_amount != unit_gross * quantity")
            _require(line_discount >= 0 and net >= 0 and tax >= 0, errors,
                     f"line {item_id} has negative discount/net/tax")
            gross_values.append(gross)
            line_discount_values.append(line_discount)
            net_values.append(net)
            tax_values.append(tax)
            parsed_lines.append((row, quantity, gross, line_discount, net, tax))

        order_gross = sum(gross_values)
        if not parsed_lines:
            errors.append(f"order {order_id} has no valid item lines")
            continue
        promo = order.get("promotion")
        try:
            threshold = to_cents(promo["threshold"]) if promo else 0
            promo_discount = to_cents(promo["discount"]) if promo else 0
        except (TypeError, ValueError) as exc:
            errors.append(f"order {order_id} invalid promotion money: {exc}")
            threshold, promo_discount = 0, 0
        _require(promo_discount >= 0 and threshold >= 0, errors,
                 f"order {order_id} promotion has negative threshold/discount")
        if promo is None or order_gross < threshold:
            _require(promo_discount == 0, errors,
                     f"order {order_id} promotion discount must be zero "
                     "when the gross total misses the threshold")
        # The order-level discount can never exceed the pre-promo margin,
        # otherwise a partial return could drive merchandise payable negative.
        _require(promo_discount <= order_gross - sum(net_values), errors,
                 f"order {order_id} promotion exceeds the pre-promo margin")

        promo_shares = allocate_by_weight(gross_values, promo_discount)
        lines: list[LineSnapshot] = []
        for (row, quantity, gross, line_discount, net, tax), promo_share in zip(
            parsed_lines, promo_shares
        ):
            expected_net = gross - line_discount - promo_share
            _require(expected_net == net, errors,
                     f"line {row['item_id']} line_total does not match the "
                     "gross-weighted promotion share")
            unit_gross_split = split_units(gross, quantity)
            unit_disc_split = split_units(line_discount, quantity)
            unit_net_split = split_units(net, quantity)
            unit_tax_split = split_units(tax, quantity)
            lines.append(LineSnapshot(
                item_id=row["item_id"], order_id=order_id,
                product_id=row["product_id"], quantity=quantity, gross=gross,
                line_discount=line_discount, promo_share=promo_share, net=net,
                tax=tax, unit_gross=unit_gross_split,
                unit_line_discount=unit_disc_split,
                unit_net=unit_net_split, unit_tax=unit_tax_split,
            ))

        payments: list[PaymentSnapshot] = []
        seen_payments: set[str] = set()
        for row in sorted(raw_payments.get(order_id, []),
                          key=lambda r: r["payment_id"]):
            _require(row["payment_id"] not in seen_payments, errors,
                     f"duplicate payment_id {row['payment_id']}")
            seen_payments.add(row["payment_id"])
            try:
                captured = to_cents(row["captured_amount"])
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"payment {row['payment_id']} invalid money: {exc}")
                continue
            _require(captured > 0, errors,
                     f"payment {row['payment_id']} captured amount must be positive")
            payments.append(PaymentSnapshot(
                payment_id=row["payment_id"], order_id=order_id, captured=captured))
        captured_total = sum(p.captured for p in payments)
        net_total = sum(net_values)
        tax_total = sum(tax_values)
        _require(captured_total == net_total + tax_total, errors,
                 f"order {order_id} captured {format_cents(captured_total)} != "
                 f"net {format_cents(net_total)} + tax {format_cents(tax_total)}")
        _require(bool(payments), errors, f"order {order_id} has no captured payments")

        default_contract = order.get("default_contract")
        _require(default_contract is None or default_contract in CONTRACTS, errors,
                 f"order {order_id} unknown default_contract {default_contract!r}")

        snapshots[order_id] = OrderSnapshot(
            order_id=order_id, status=order["status"], threshold=threshold,
            promo_discount=promo_discount, default_contract=default_contract,
            lines=tuple(lines), payments=tuple(payments), gross=order_gross,
            total_line_discount=sum(line_discount_values), net_total=net_total,
            tax_total=tax_total, captured_total=captured_total)

    for row in fixture.get("items", []):
        if row["order_id"] not in raw_orders:
            errors.append(f"line {row['item_id']} references unknown order "
                          f"{row['order_id']}")
    for row in fixture.get("payments", []):
        if row["order_id"] not in raw_orders:
            errors.append(f"payment {row['payment_id']} references unknown order "
                          f"{row['order_id']}")
    if errors:
        raise SettlementError("settlement snapshot audit failed: " + "; ".join(errors))
    return snapshots


# --------------------------------------------------------------------------- #
# Cumulative recomputation over the currently settled unit set
# --------------------------------------------------------------------------- #
def _active_totals(snapshot: OrderSnapshot,
                   active_units: dict[str, list[int]]) -> dict[str, Any]:
    returned_gross = 0
    line_disc_returned = 0
    tax_returned = 0
    per_line: dict[str, dict[str, int]] = {}
    for line in snapshot.lines:
        indices = active_units.get(line.item_id, [])
        gross = sum(line.unit_gross[i] for i in indices)
        disc = sum(line.unit_line_discount[i] for i in indices)
        tax = sum(line.unit_tax[i] for i in indices)
        returned_gross += gross
        line_disc_returned += disc
        tax_returned += tax
        per_line[line.item_id] = {
            "units": len(indices), "gross": gross,
            "line_discount": disc, "tax": tax,
        }
    return {
        "returned_gross": returned_gross,
        "line_discount_returned": line_disc_returned,
        "tax_returned": tax_returned,
        "per_line": per_line,
    }


def recompute_cumulative(snapshot: OrderSnapshot,
                         active_units: dict[str, list[int]],
                         contract: str) -> dict[str, int]:
    """Cumulative payable for every currently settled return, as one set.

    Never computed per request in isolation: the promotion is always
    re-evaluated against the whole settled set, so totals can never drift
    past the originally captured amount.
    """
    totals = _active_totals(snapshot, active_units)
    returned_gross = totals["returned_gross"]
    promo = snapshot.promo_discount
    if contract == RETAIN_PROMOTION:
        # Floor so the kept goods carry the remainder cent; the full-return
        # case still yields the whole promotion exactly.
        promo_returned = (
            promo * returned_gross // snapshot.gross if snapshot.gross else 0
        )
    elif contract == REPRICE_KEPT:
        kept_gross = snapshot.gross - returned_gross
        promo_kept = promo if kept_gross >= snapshot.threshold else 0
        promo_returned = promo - promo_kept
    else:  # pragma: no cover - callers validate the contract
        raise SettlementError(f"unknown promotion contract {contract!r}")

    merchandise = (
        returned_gross - totals["line_discount_returned"] - promo_returned
    )
    if merchandise < 0 or promo_returned < 0 or promo_returned > promo:
        raise SettlementInvariantError(
            f"order {snapshot.order_id}: promotion clawback produced an "
            "impossible merchandise total")
    tax_returned = totals["tax_returned"]
    return {
        "returned_gross": returned_gross,
        "line_discount_returned": totals["line_discount_returned"],
        "promotion_returned": promo_returned,
        "merchandise": merchandise,
        "tax": tax_returned,
        "total": merchandise + tax_returned,
    }


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
@dataclass
class _ClaimRecord:
    request_id: str
    payload_hash: str
    order_id: str
    item_id: Optional[str]
    quantity: int
    route: str
    contract: Optional[str]
    units: list[int]
    amount: int
    revoked: bool = False
    decision: dict[str, Any] = field(default_factory=dict)


class SettlementLedger:
    """Stateful, idempotent settlement ledger for one fixture's orders."""

    def __init__(self, snapshots: dict[str, OrderSnapshot]):
        self.snapshots = snapshots
        self._active: dict[str, dict[str, list[int]]] = {
            oid: {line.item_id: [] for line in snap.lines}
            for oid, snap in snapshots.items()
        }
        self._remaining: dict[str, dict[str, int]] = {
            oid: {p.payment_id: p.captured for p in snap.payments}
            for oid, snap in snapshots.items()
        }
        self._contract: dict[str, Optional[str]] = {
            oid: None for oid in snapshots
        }
        self._claims: dict[str, _ClaimRecord] = {}
        self._liabilities: dict[str, dict[str, int]] = {
            oid: {} for oid in snapshots
        }
        self.decisions: list[dict[str, Any]] = []

    # -- construction ----------------------------------------------------- #
    @classmethod
    def from_fixture(cls, fixture: dict[str, Any]) -> "SettlementLedger":
        snapshots = build_snapshots(fixture)
        ledger = cls(snapshots)
        ledger._ingest_prior_refunds(fixture.get("prior_refunds", []))
        return ledger

    def _ingest_prior_refunds(self, prior_refunds: list[dict[str, Any]]) -> None:
        errors: list[str] = []
        # Phase 1: validate references, quantities, routes and the fixed
        # contract before mutating any ledger state.
        staged: list[tuple[dict[str, Any], OrderSnapshot, LineSnapshot,
                           list[int], int, str]] = []
        for row in prior_refunds:
            refund_id = row["refund_id"]
            order = self.snapshots.get(row["order_id"])
            if order is None:
                errors.append(f"prior refund {refund_id} references unknown order")
                continue
            line = order.line(row["item_id"])
            if line is None:
                errors.append(f"prior refund {refund_id} references unknown line")
                continue
            contract = row.get("promotion_contract") or order.default_contract
            if contract not in CONTRACTS:
                errors.append(f"prior refund {refund_id} lacks a valid contract")
                continue
            fixed = self._contract[order.order_id]
            if fixed is not None and contract != fixed:
                errors.append(
                    f"prior refund {refund_id} contract {contract!r} conflicts "
                    f"with fixed contract {fixed!r}")
                continue
            quantity = _positive_int(row["quantity"],
                                     f"prior refund {refund_id} quantity")
            used = len(self._active[order.order_id][line.item_id])
            if quantity <= 0 or used + quantity > line.quantity:
                errors.append(f"prior refund {refund_id} exceeds line quantity")
                continue
            route = row["route"]
            if route not in ROUTES:
                errors.append(f"prior refund {refund_id} has unknown route {route!r}")
                continue
            try:
                amount = to_cents(row["amount"])
            except (TypeError, ValueError) as exc:
                errors.append(f"prior refund {refund_id} invalid money: {exc}")
                continue
            if amount <= 0:
                errors.append(f"prior refund {refund_id} amount must be positive")
                continue
            indices = list(range(used, used + quantity))
            staged.append((row, order, line, indices, amount, contract))
        if errors:
            raise SettlementError("prior refund audit failed: " + "; ".join(errors))

        # Phase 2: apply instrument/liability movements in fixture order.
        staged_by_order: dict[str, int] = {}
        for row, order, line, indices, amount, contract in staged:
            order_id = order.order_id
            if row["route"] == CASH:
                spread = self._allocate_cash(order_id, amount,
                                             row.get("instrument_id"))
                if spread is None:
                    raise SettlementError(
                        f"prior cash refund {row['refund_id']} exceeds the "
                        "remaining original instrument balances")
                for payment_id, part in spread:
                    self._remaining[order_id][payment_id] -= part
            else:
                self._liabilities[order_id][row["refund_id"]] = amount
            self._active[order_id][line.item_id].extend(indices)
            self._contract[order_id] = contract
            staged_by_order[order_id] = staged_by_order.get(order_id, 0) + amount

        # Phase 3: the historical movements must equal the cumulative payable
        # recomputed over the whole settled unit set, never per-row caps.
        for order_id, recorded_total in staged_by_order.items():
            snap = self.snapshots[order_id]
            cumulative = recompute_cumulative(
                snap, self._active[order_id], self._contract[order_id])
            if recorded_total != cumulative["total"]:
                raise SettlementError(
                    f"prior refunds on order {order_id} total "
                    f"{format_cents(recorded_total)} but the original sale "
                    f"snapshot recomputes {format_cents(cumulative['total'])}")
            if cumulative["total"] > snap.captured_total:
                raise SettlementError(
                    f"prior refunds on order {order_id} exceed the captured total")

    # -- cash allocation -------------------------------------------------- #
    def _allocate_cash(self, order_id: str, amount: int,
                       instrument_id: Optional[str]) -> Optional[list[tuple[str, int]]]:
        remaining = self._remaining[order_id]
        if instrument_id is not None:
            if instrument_id not in remaining or remaining[instrument_id] < amount:
                return None
            return [(instrument_id, amount)]
        instructions: list[tuple[str, int]] = []
        left = amount
        for payment_id, balance in remaining.items():
            part = min(left, balance)
            if part:
                instructions.append((payment_id, part))
                left -= part
            if left == 0:
                break
        if left:
            return None
        return instructions

    # -- public API ------------------------------------------------------- #
    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["request_id"]
        order_id = request["order_id"]
        digest = _payload_hash(request)

        previous = self._claims.get(request_id)
        if previous is not None:
            if previous.payload_hash != digest:
                decision = self._reject(request_id, order_id, "payload_conflict")
                decision["replay"] = True
                return decision
            replay = dict(previous.decision)
            replay["replay"] = True
            self.decisions.append(replay)
            return replay

        order = self.snapshots.get(order_id)
        if order is None:
            return self._record_reject(request, "unknown_order")

        if request.get("op", "return") == "revoke":
            return self._revoke(request, digest, order)

        line = order.line(request["item_id"]) if "item_id" in request else None
        quantity = _positive_int(request.get("quantity", 0), "request quantity")
        if line is None:
            return self._record_reject(request, "unknown_line")
        used = len(self._active[order_id][line.item_id])
        if order.status not in ("paid", "shipped"):
            return self._record_reject(request, "unpaid_order")
        if quantity <= 0 or used + quantity > line.quantity:
            return self._record_reject(request, "quantity_exceeded")

        contract = request.get("promotion_contract") or order.default_contract
        if contract is None:
            return self._record_reject(request, "contract_unspecified")
        if contract not in CONTRACTS:
            return self._record_reject(request, "unknown_contract")
        fixed = self._contract[order_id]
        if fixed is not None and contract != fixed:
            return self._record_reject(request, "contract_conflict")

        route = request.get("route", CASH)
        if route not in ROUTES:
            return self._record_reject(request, "unknown_route")
        instrument_id = request.get("instrument_id")
        if route != CASH and instrument_id is not None:
            return self._record_reject(request, "instrument_not_applicable")

        indices = list(range(used, used + quantity))
        before = recompute_cumulative(order, self._active[order_id], contract)
        tentative = dict(self._active[order_id])
        tentative[line.item_id] = tentative[line.item_id] + indices
        after = recompute_cumulative(order, tentative, contract)
        delta_total = after["total"] - before["total"]
        if delta_total <= 0:
            raise SettlementInvariantError(
                f"order {order_id}: request {request_id} produced non-positive "
                "payable delta")
        if after["total"] > order.captured_total:
            raise SettlementInvariantError(
                f"order {order_id}: cumulative payable exceeds captured total")

        cash_instructions = None
        if route == CASH:
            cash_instructions = self._allocate_cash(
                order_id, delta_total, instrument_id)
            if cash_instructions is None:
                return self._record_reject(request, "instrument_capacity_exceeded")
            for payment_id, part in cash_instructions:
                self._remaining[order_id][payment_id] -= part
        else:
            self._liabilities[order_id][request_id] = delta_total

        self._contract[order_id] = contract
        self._active[order_id] = tentative
        record = _ClaimRecord(
            request_id=request_id, order_id=order_id, item_id=line.item_id,
            payload_hash=digest, quantity=quantity, route=route,
            contract=contract, units=indices, amount=delta_total)
        decision = {
            "request_id": request_id,
            "order_id": order_id,
            "claim_id": request_id,
            "item_id": line.item_id,
            "status": "accepted",
            "reason": None,
            "op": "return",
            "route": route,
            "promotion_contract": contract,
            "replay": False,
            "units": [{"item_id": line.item_id, "unit_index": i} for i in indices],
            "returned_gross": format_cents(
                after["returned_gross"] - before["returned_gross"]),
            "line_discount_refund": format_cents(
                after["line_discount_returned"] - before["line_discount_returned"]),
            "promotion_clawback": format_cents(
                after["promotion_returned"] - before["promotion_returned"]),
            "merchandise_refund": format_cents(
                after["merchandise"] - before["merchandise"]),
            "tax_refund": format_cents(after["tax"] - before["tax"]),
            "cash_refund": format_cents(delta_total) if route == CASH else "0.00",
            "credit_refund": format_cents(delta_total)
            if route == STORE_CREDIT else "0.00",
            "cash_instructions": [
                {"payment_id": pid, "amount": format_cents(part)}
                for pid, part in (cash_instructions or [])
            ],
            "cumulative_payable": format_cents(after["total"]),
        }
        record.decision = dict(decision)
        self._claims[request_id] = record
        self.decisions.append(decision)
        return decision

    def _revoke(self, request: dict[str, Any], digest: str,
                order: OrderSnapshot) -> dict[str, Any]:
        request_id = request["request_id"]
        target_id = request.get("target_request_id")
        target = self._claims.get(target_id)
        if (target is None or target.order_id != order.order_id or target.revoked
                or target.item_id is None or not target.units):
            return self._record_reject(request, "revocation_target_inactive")
        if target.decision.get("op") != "return" or target.decision["status"] != "accepted":
            return self._record_reject(request, "revocation_target_inactive")

        contract = target.contract
        assert contract is not None
        active = self._active[order.order_id]
        active[target.item_id] = [
            i for i in active[target.item_id] if i not in target.units
        ]
        cumulative = recompute_cumulative(order, active, contract)

        credit_voided = 0
        cash_recovered: list[tuple[str, int]] = []
        if target.route == STORE_CREDIT:
            credit_voided = self._liabilities[order.order_id].pop(target_id, 0)
            if credit_voided != target.amount:
                raise SettlementInvariantError(
                    f"order {order.order_id}: credit liability {target_id} "
                    "does not match the recorded settlement")
        else:
            # Reverse the exact per-instrument instructions of the original
            # cash settlement, so each instrument is restored audited-cent
            # for audited-cent and can never exceed its captured amount.
            for instruction in target.decision["cash_instructions"]:
                payment_id = instruction["payment_id"]
                part = to_cents(instruction["amount"])
                self._remaining[order.order_id][payment_id] += part
                cash_recovered.append((payment_id, part))
            if sum(part for _, part in cash_recovered) != target.amount:
                raise SettlementInvariantError(
                    f"order {order.order_id}: revoked cash instructions do not "
                    "match the recorded settlement")
        target.revoked = True
        decision = {
            "request_id": request_id,
            "order_id": order.order_id,
            "claim_id": request_id,
            "item_id": target.item_id,
            "status": "accepted",
            "reason": None,
            "op": "revoke",
            "route": target.route,
            "promotion_contract": contract,
            "replay": False,
            "target_request_id": target_id,
            "cash_refund": "0.00",
            "credit_refund": "0.00",
            "cash_recovered": [
                {"payment_id": pid, "amount": format_cents(part)}
                for pid, part in cash_recovered
            ],
            "credit_voided": format_cents(credit_voided),
            "cash_instructions": [],
            "cumulative_payable": format_cents(cumulative["total"]),
        }
        record = _ClaimRecord(
            request_id=request_id, order_id=order.order_id,
            item_id=target.item_id, payload_hash=digest,
            quantity=target.quantity, route=target.route, contract=contract,
            units=[], amount=-target.amount, decision=dict(decision))
        self._claims[request_id] = record
        self.decisions.append(decision)
        return decision

    # -- rejection bookkeeping ------------------------------------------- #
    def _record_reject(self, request: dict[str, Any], reason: str) -> dict[str, Any]:
        decision = self._reject(
            request["request_id"], request["order_id"], reason)
        self._claims[request["request_id"]] = _ClaimRecord(
            request_id=request["request_id"],
            payload_hash=_payload_hash(request),
            order_id=request["order_id"], item_id=request.get("item_id"),
            quantity=_safe_int(request.get("quantity", 0)),
            route=request.get("route", CASH),
            contract=request.get("promotion_contract"),
            units=[], amount=0, decision=dict(decision))
        self.decisions.append(decision)
        return decision

    @staticmethod
    def _reject(request_id: str, order_id: str, reason: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "order_id": order_id,
            "claim_id": request_id,
            "status": "rejected",
            "reason": reason,
            "replay": False,
        }

    # -- serialization ---------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "orders": {
                order_id: {
                    "promotion_contract": self._contract[order_id],
                    "active_units": {
                        line.item_id: list(self._active[order_id][line.item_id])
                        for line in snap.lines
                    },
                    "instrument_remaining": {
                        pid: format_cents(cents)
                        for pid, cents in self._remaining[order_id].items()
                    },
                    "credit_liabilities": {
                        rid: format_cents(cents)
                        for rid, cents in self._liabilities[order_id].items()
                    },
                }
                for order_id, snap in self.snapshots.items()
            },
            "requests": {
                rid: {
                    "payload_hash": rec.payload_hash,
                    "revoked": rec.revoked,
                    "route": rec.route,
                    "amount": format_cents(rec.amount),
                }
                for rid, rec in self._claims.items()
            },
        }

    def order_summary(self, order_id: str) -> dict[str, Any]:
        snap = self.snapshots[order_id]
        contract = self._contract[order_id]
        if contract is None:
            cumulative = {
                "returned_gross": 0, "line_discount_returned": 0,
                "promotion_returned": 0, "merchandise": 0, "tax": 0, "total": 0,
            }
        else:
            cumulative = recompute_cumulative(
                snap, self._active[order_id], contract)
        return {
            "order_id": order_id,
            "promotion_contract": contract,
            "captured_total": format_cents(snap.captured_total),
            "cumulative_payable": format_cents(cumulative["total"]),
            "cumulative_merchandise": format_cents(cumulative["merchandise"]),
            "cumulative_tax": format_cents(cumulative["tax"]),
            "promotion_clawed_back": format_cents(cumulative["promotion_returned"]),
            "instrument_remaining": {
                pid: format_cents(cents)
                for pid, cents in self._remaining[order_id].items()
            },
            "credit_liabilities": {
                rid: format_cents(cents)
                for rid, cents in self._liabilities[order_id].items()
            },
            "active_units": {
                line.item_id: list(self._active[order_id][line.item_id])
                for line in snap.lines
            },
        }


def settle_fixture(fixture: dict[str, Any],
                   ledger: Optional[SettlementLedger] = None) -> dict[str, Any]:
    """Audit the snapshot, then settle every request in fixture order."""
    snapshots = build_snapshots(fixture)
    if ledger is None:
        ledger = SettlementLedger.from_fixture(fixture)
    decisions = [ledger.submit(request) for request in fixture.get("requests", [])]
    return {
        "audit": {
            "status": "coherent",
            "orders": [
                {
                    "order_id": oid,
                    "gross_amount": format_cents(snap.gross),
                    "line_discount_total": format_cents(snap.total_line_discount),
                    "promotion_discount": format_cents(snap.promo_discount),
                    "net_total": format_cents(snap.net_total),
                    "tax_total": format_cents(snap.tax_total),
                    "captured_total": format_cents(snap.captured_total),
                    "payment_count": len(snap.payments),
                }
                for oid, snap in snapshots.items()
            ],
        },
        "decisions": decisions,
        "orders": [ledger.order_summary(oid) for oid in snapshots],
        "ledger": ledger.to_dict(),
    }
