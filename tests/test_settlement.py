"""Isolated tests for downstream return settlement."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from synthlab.commerce_audit import audit_settlement_snapshot
from synthlab.money import allocate_by_weight, format_cents, split_units, to_cents
from synthlab.return_intake import assess_returns, evaluate_claim, run_settlement
from synthlab.settlement import (
    REPRICE_KEPT,
    RETAIN_PROMOTION,
    SettlementError,
    SettlementLedger,
    recompute_cumulative,
    settle_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "returns" / "settlement.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def decisions_by_id(result):
    return {d["request_id"]: d for d in result["decisions"]}


def settle(requests, fixture=None):
    fixture = copy.deepcopy(fixture or load_fixture())
    fixture["requests"] = requests
    return settle_fixture(fixture)


def canonical_order():
    """The spec's canonical 65 yuan / 5 yuan off over 60 order."""
    fixture = load_fixture()
    fixture["requests"] = []
    return fixture


# --------------------------------------------------------------------------- #
# Exact arithmetic primitives
# --------------------------------------------------------------------------- #
def test_float_is_rejected_and_decimal_strings_are_exact():
    assert to_cents("23.08") == 2308
    assert to_cents(100) == 100
    assert format_cents(2308) == "23.08"
    with pytest.raises(TypeError):
        to_cents(23.08)
    with pytest.raises(ValueError):
        to_cents("23.081")


def test_remainder_cents_follow_stable_unit_indices():
    assert split_units(1000, 3) == [334, 333, 333]
    assert sum(split_units(1000, 3)) == 1000
    assert split_units(999, 3) == [333, 333, 333]
    assert allocate_by_weight([2500, 4000], 500) == [192, 308]
    assert sum(allocate_by_weight([1, 1, 1], 10)) == 10
    # Stable tie-break: earlier indices win the leftover cents.
    assert allocate_by_weight([1, 1, 1], 7) == [3, 2, 2]


# --------------------------------------------------------------------------- #
# The two authorized promotion contracts on one identical input
# --------------------------------------------------------------------------- #
def _canonical_request(contract):
    return [{
        "request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
        "quantity": 1, "route": "cash", "promotion_contract": contract,
    }]


def test_retain_promotion_refunds_transaction_net_of_the_returned_unit():
    result = settle(_canonical_request(RETAIN_PROMOTION))
    decision = result["decisions"][0]
    # 25.00 gross - 1.92 promotion share + 1.85 tax = 24.93 cash.
    assert decision["merchandise_refund"] == "23.08"
    assert decision["tax_refund"] == "1.85"
    assert decision["cash_refund"] == "24.93"
    assert decision["promotion_clawback"] == "1.92"


def test_reprice_kept_claws_back_the_kept_line_s_lost_benefit():
    result = settle(_canonical_request(REPRICE_KEPT))
    decision = result["decisions"][0]
    # Kept 40 yuan line drops below 60: it loses its 3.08 share, so the
    # returned line only gets 20.00 merchandise back, plus its own tax.
    assert decision["merchandise_refund"] == "20.00"
    assert decision["tax_refund"] == "1.85"
    assert decision["cash_refund"] == "21.85"
    assert decision["promotion_clawback"] == "5.00"


def test_two_contracts_produce_different_payouts_on_the_same_input():
    retained = settle(_canonical_request(RETAIN_PROMOTION))["decisions"][0]
    repriced = settle(_canonical_request(REPRICE_KEPT))["decisions"][0]
    assert retained["cash_refund"] != repriced["cash_refund"]
    assert (retained["cash_refund"], repriced["cash_refund"]) == ("24.93", "21.85")
    # Neither contract ever edits the original sale or tax vouchers.
    fixture = load_fixture()
    line = next(row for row in fixture["items"] if row["item_id"] == "i-100a")
    assert line["line_total"] == "23.08"
    assert line["tax_amount"] == "1.85"


def test_tax_refund_keeps_original_per_line_tax_units_under_clawback():
    result = settle(_canonical_request(REPRICE_KEPT))
    decision = result["decisions"][0]
    # Even when the whole 5.00 promotion is clawed back, tax stays at 1.85:
    # promotion clawback must not fabricate a merchandise tax adjustment.
    assert decision["tax_refund"] == "1.85"
    assert decision["promotion_clawback"] == "5.00"


# --------------------------------------------------------------------------- #
# Remainder allocation across the 3-unit / 10.00 line
# --------------------------------------------------------------------------- #
def test_three_unit_line_remainder_sums_to_the_full_line_in_any_order():
    requests = [
        {"request_id": f"q-{i}", "order_id": "o-200", "item_id": "i-200",
         "quantity": 1, "route": "cash"}
        for i in range(3)
    ]
    result = settle(requests)
    payouts = [d["cash_refund"] for d in result["decisions"]]
    assert sorted(payouts) == ["3.60", "3.60", "3.61"]
    summary = next(o for o in result["orders"] if o["order_id"] == "o-200")
    # Full line returned: every cent of net 10.00 + tax 0.81 is settled.
    assert summary["cumulative_payable"] == "10.81"
    assert summary["instrument_remaining"] == {"p-200": "0.00"}


def test_cumulative_total_never_exceeds_captured_amount():
    requests = [
        {"request_id": "q-a", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1, "route": "cash", "promotion_contract": RETAIN_PROMOTION},
        {"request_id": "q-b", "order_id": "o-100", "item_id": "i-100b",
         "quantity": 1, "route": "cash", "promotion_contract": RETAIN_PROMOTION},
    ]
    result = settle(requests)
    summary = next(o for o in result["orders"] if o["order_id"] == "o-100")
    assert summary["cumulative_payable"] == "64.80"
    assert summary["captured_total"] == "64.80"
    assert all(v == "0.00" for v in summary["instrument_remaining"].values())


# --------------------------------------------------------------------------- #
# Two capture instruments: audited per-instrument cash instructions
# --------------------------------------------------------------------------- #
def test_cash_spreads_across_each_original_instrument_s_open_balance():
    result = settle([
        {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1, "route": "cash",
         "promotion_contract": RETAIN_PROMOTION},
    ])
    decision = result["decisions"][0]
    assert decision["cash_instructions"] == [
        {"payment_id": "p-100-1", "amount": "24.93"}]
    summary = next(o for o in result["orders"] if o["order_id"] == "o-100")
    assert summary["instrument_remaining"] == {
        "p-100-1": "5.07", "p-100-2": "34.80"}


def test_targeted_instrument_with_insufficient_balance_is_all_or_nothing():
    # p-500-1 holds 8.00 and the prior refund already consumed all of it.
    result = settle([
        {"request_id": "q-1", "order_id": "o-500", "item_id": "i-500",
         "quantity": 1, "route": "cash", "instrument_id": "p-500-1"},
    ])
    decision = result["decisions"][0]
    assert decision["status"] == "rejected"
    assert decision["reason"] == "instrument_capacity_exceeded"
    # No partial success: nothing was reserved.
    summary = next(o for o in result["orders"] if o["order_id"] == "o-500")
    assert summary["instrument_remaining"] == {
        "p-500-1": "0.00", "p-500-2": "12.42"}


def test_same_request_succeeds_through_other_instrument_after_rejection():
    result = settle([
        {"request_id": "q-1", "order_id": "o-500", "item_id": "i-500",
         "quantity": 1, "route": "cash"},
    ])
    decision = result["decisions"][0]
    assert decision["status"] == "accepted"
    assert decision["cash_instructions"] == [
        {"payment_id": "p-500-2", "amount": "12.42"}]


# --------------------------------------------------------------------------- #
# Store credit: recorded liability that blocks a second cash settlement
# --------------------------------------------------------------------------- #
def test_store_credit_records_liability_and_leaves_instruments_untouched():
    result = settle([
        {"request_id": "q-1", "order_id": "o-600", "item_id": "i-600",
         "quantity": 1, "route": "store_credit"},
    ])
    decision = result["decisions"][0]
    assert decision["credit_refund"] == "12.42"
    assert decision["cash_refund"] == "0.00"
    assert decision["cash_instructions"] == []
    summary = next(o for o in result["orders"] if o["order_id"] == "o-600")
    assert summary["credit_liabilities"] == {"q-1": "12.42"}
    assert summary["instrument_remaining"] == {
        "p-600-1": "12.42", "p-600-2": "12.42"}


def test_credit_then_cash_claim_does_not_pay_twice_for_the_same_units():
    result = settle([
        {"request_id": "q-1", "order_id": "o-200", "item_id": "i-200",
         "quantity": 1, "route": "store_credit"},
        {"request_id": "q-2", "order_id": "o-200", "item_id": "i-200",
         "quantity": 1, "route": "cash"},
    ])
    first, second = result["decisions"]
    assert first["credit_refund"] == "3.60"
    # The cash request settles a *different* unit; the credited unit never
    # also receives cash.
    assert second["status"] == "accepted"
    assert second["units"][0]["unit_index"] == 1
    summary = next(o for o in result["orders"] if o["order_id"] == "o-200")
    assert sum(to_cents(v) for v in summary["credit_liabilities"].values()) == 360


# --------------------------------------------------------------------------- #
# Idempotency, payload conflicts and quantity non-reuse
# --------------------------------------------------------------------------- #
def test_replay_same_id_same_payload_returns_the_same_decision():
    request = {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
               "quantity": 1, "route": "cash",
               "promotion_contract": RETAIN_PROMOTION}
    fixture = canonical_order()
    ledger = SettlementLedger.from_fixture(fixture)
    first = ledger.submit(copy.deepcopy(request))
    second = ledger.submit(copy.deepcopy(request))
    assert first["request_id"] == second["request_id"]
    assert second["replay"] is True
    assert {k: second[k] for k in
            ("status", "cash_refund", "cash_instructions")} == {
        "status": first["status"], "cash_refund": first["cash_refund"],
        "cash_instructions": first["cash_instructions"]}
    # Replay must not consume another unit.
    summary = ledger.order_summary("o-100")
    assert summary["active_units"]["i-100a"] == [0]


def test_same_id_with_different_payload_is_rejected_not_replayed():
    request = {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
               "quantity": 1, "route": "cash",
               "promotion_contract": RETAIN_PROMOTION}
    ledger = SettlementLedger.from_fixture(canonical_order())
    ledger.submit(copy.deepcopy(request))
    tampered = copy.deepcopy(request)
    tampered["route"] = "store_credit"
    conflict = ledger.submit(tampered)
    assert conflict["status"] == "rejected"
    assert conflict["reason"] == "payload_conflict"
    assert conflict["replay"] is True


def test_quantity_cannot_be_occupied_twice_and_intake_rule_is_shared():
    fixture = load_fixture()
    result = settle_fixture(fixture)
    decisions = decisions_by_id(result)
    assert decisions["q-200-4"]["reason"] == "quantity_exceeded"
    assert decisions["q-700-1"]["reason"] == "unpaid_order"
    # The shared eligibility helper makes the legacy intake reach the same
    # verdict on equivalent rows.
    order = {"order_id": "o-200", "status": "shipped"}
    item = {"item_id": "i-200", "order_id": "o-200", "quantity": 3}
    assert evaluate_claim(order, item,
                          {"order_id": "o-200", "item_id": "i-200",
                           "quantity": 1}, {"i-200": 3}) == "quantity_exceeded"


# --------------------------------------------------------------------------- #
# Contract fixation and threshold-crossing re-evaluation
# --------------------------------------------------------------------------- #
def test_contract_is_fixed_at_first_settlement_and_cannot_be_switched():
    result = settle([
        {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1, "route": "cash",
         "promotion_contract": RETAIN_PROMOTION},
        {"request_id": "q-2", "order_id": "o-100", "item_id": "i-100b",
         "quantity": 1, "route": "cash",
         "promotion_contract": REPRICE_KEPT},
    ])
    decisions = result["decisions"]
    assert decisions[0]["status"] == "accepted"
    assert decisions[1]["status"] == "rejected"
    assert decisions[1]["reason"] == "contract_conflict"


def test_reprice_threshold_crossing_recomputes_cumulative_not_per_request():
    # First return drops kept gross to 40.00 (< 60.00 threshold); the second
    # request must be evaluated against the whole settled set, so totals
    # still converge on the captured amount.
    result = settle([
        {"request_id": "q-1", "order_id": "o-400", "item_id": "i-400a",
         "quantity": 1, "route": "cash"},
        {"request_id": "q-2", "order_id": "o-400", "item_id": "i-400b",
         "quantity": 1, "route": "cash"},
    ])
    first, second = result["decisions"]
    assert first["cash_refund"] == "21.85"   # 20.00 net + 1.85 tax
    assert second["cash_refund"] == "42.95"  # delta to full 64.80
    summary = next(o for o in result["orders"] if o["order_id"] == "o-400")
    assert summary["cumulative_payable"] == "64.80"
    assert summary["promotion_clawed_back"] == "5.00"


def test_reprice_when_threshold_still_met_keeps_full_promotion_on_kept_goods():
    # 80.00 gross, 5.00 off over 60.00; returning a 20.00 line leaves 60.00,
    # so the kept goods retain the whole promotion and the returned unit
    # receives no promotion benefit at all.
    fixture = canonical_order()
    fixture["orders"].append({
        "order_id": "o-800", "status": "paid",
        "default_contract": REPRICE_KEPT,
        "promotion": {"threshold": "60.00", "discount": "5.00"}})
    fixture["items"] = fixture["items"] + [
        {"item_id": "i-800a", "order_id": "o-800", "product_id": "sku-a",
         "quantity": 1, "unit_gross": "20.00", "gross_amount": "20.00",
         "line_discount": "0.00", "line_total": "18.75", "tax_amount": "1.60"},
        {"item_id": "i-800b", "order_id": "o-800", "product_id": "sku-b",
         "quantity": 1, "unit_gross": "60.00", "gross_amount": "60.00",
         "line_discount": "0.00", "line_total": "56.25", "tax_amount": "4.40"},
    ]
    fixture["payments"] = fixture["payments"] + [
        {"payment_id": "p-800", "order_id": "o-800",
         "captured_amount": "81.00"}]
    result = settle([
        {"request_id": "q-1", "order_id": "o-800", "item_id": "i-800a",
         "quantity": 1, "route": "cash"}], fixture)
    decision = result["decisions"][0]
    assert decision["merchandise_refund"] == "20.00"
    assert decision["promotion_clawback"] == "0.00"
    assert decision["tax_refund"] == "1.60"


# --------------------------------------------------------------------------- #
# Revocation and re-settlement
# --------------------------------------------------------------------------- #
def test_revocation_releases_units_and_allows_cash_resettlement():
    result = settle([
        {"request_id": "q-1", "order_id": "o-600", "item_id": "i-600",
         "quantity": 1, "route": "store_credit"},
        {"request_id": "q-r", "order_id": "o-600", "op": "revoke",
         "target_request_id": "q-1"},
        {"request_id": "q-2", "order_id": "o-600", "item_id": "i-600",
         "quantity": 1, "route": "cash"},
    ])
    decisions = decisions_by_id(result)
    assert decisions["q-r"]["op"] == "revoke"
    assert decisions["q-r"]["credit_voided"] == "12.42"
    assert decisions["q-r"]["cumulative_payable"] == "0.00"
    assert decisions["q-2"]["status"] == "accepted"
    assert decisions["q-2"]["cash_refund"] == "12.42"
    summary = next(o for o in result["orders"] if o["order_id"] == "o-600")
    assert summary["credit_liabilities"] == {}
    assert summary["cumulative_payable"] == "12.42"


def test_cash_revocation_restores_exact_instruction_amounts():
    result = settle([
        {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1, "route": "cash",
         "promotion_contract": RETAIN_PROMOTION},
        {"request_id": "q-r", "order_id": "o-100", "op": "revoke",
         "target_request_id": "q-1"},
    ])
    revoke = result["decisions"][1]
    assert revoke["cash_recovered"] == [
        {"payment_id": "p-100-1", "amount": "24.93"}]
    summary = next(o for o in result["orders"] if o["order_id"] == "o-100")
    assert summary["instrument_remaining"] == {
        "p-100-1": "30.00", "p-100-2": "34.80"}
    assert summary["active_units"]["i-100a"] == []


def test_revoking_an_unknown_or_already_revoked_request_is_rejected():
    result = settle([
        {"request_id": "q-1", "order_id": "o-600", "item_id": "i-600",
         "quantity": 1, "route": "store_credit"},
        {"request_id": "q-r1", "order_id": "o-600", "op": "revoke",
         "target_request_id": "q-1"},
        {"request_id": "q-r2", "order_id": "o-600", "op": "revoke",
         "target_request_id": "q-1"},
        {"request_id": "q-r3", "order_id": "o-600", "op": "revoke",
         "target_request_id": "missing"},
    ])
    decisions = decisions_by_id(result)
    assert decisions["q-r2"]["reason"] == "revocation_target_inactive"
    assert decisions["q-r3"]["reason"] == "revocation_target_inactive"


# --------------------------------------------------------------------------- #
# Same SKU across orders and prior-refund ledger continuity
# --------------------------------------------------------------------------- #
def test_same_sku_across_orders_settles_independently():
    result = settle([
        {"request_id": "q-1", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1, "route": "cash"},
        {"request_id": "q-2", "order_id": "o-300", "item_id": "i-300",
         "quantity": 1, "route": "cash"},
    ])
    decisions = result["decisions"]
    assert decisions[0]["cash_refund"] == "24.93"
    assert decisions[1]["cash_refund"] == "19.44"
    summaries = {o["order_id"]: o for o in result["orders"]}
    assert summaries["o-100"]["active_units"]["i-100a"] == [0]
    assert summaries["o-300"]["active_units"]["i-300"] == [0]


def test_prior_refund_is_audited_against_the_snapshot_and_blocks_overuse():
    result = settle([
        {"request_id": "q-1", "order_id": "o-500", "item_id": "i-500",
         "quantity": 1, "route": "cash", "instrument_id": "p-500-1"},
        {"request_id": "q-2", "order_id": "o-500", "item_id": "i-500",
         "quantity": 1, "route": "cash"},
    ])
    decisions = decisions_by_id(result)
    # The historical refund already consumed unit 0 and drained p-500-1.
    assert decisions["q-1"]["reason"] == "instrument_capacity_exceeded"
    assert decisions["q-2"]["status"] == "accepted"
    assert decisions["q-2"]["units"][0]["unit_index"] == 1
    summary = next(o for o in result["orders"] if o["order_id"] == "o-500")
    assert summary["active_units"]["i-500"] == [0, 1]
    assert summary["instrument_remaining"]["p-500-1"] == "0.00"


def test_tampered_prior_refund_amount_aborts_ledger_construction():
    fixture = load_fixture()
    fixture["prior_refunds"][0]["amount"] = "12.00"
    with pytest.raises(SettlementError, match="prior refunds"):
        SettlementLedger.from_fixture(fixture)


# --------------------------------------------------------------------------- #
# Snapshot audit linkage: broken upstream rows cannot be hidden by reports
# --------------------------------------------------------------------------- #
def test_snapshot_audit_accepts_the_fixture_and_aborts_on_tampering():
    fixture = load_fixture()
    assert audit_settlement_snapshot(fixture)["status"] == "coherent"
    broken = copy.deepcopy(fixture)
    broken["items"][0]["line_total"] = "22.00"
    audit = audit_settlement_snapshot(broken)
    assert audit["status"] == "inconsistent"
    assert audit["errors"]
    with pytest.raises(SettlementError, match="original-sale audit"):
        run_settlement(broken)


def test_captured_total_mismatch_is_an_audit_error():
    fixture = load_fixture()
    fixture["payments"][0]["captured_amount"] = "29.99"
    audit = audit_settlement_snapshot(fixture)
    assert audit["status"] == "inconsistent"
    assert any("captured" in err for err in audit["errors"])


def test_promotion_below_threshold_with_discount_is_rejected():
    fixture = canonical_order()
    fixture["orders"][0]["promotion"] = {
        "threshold": "90.00", "discount": "5.00"}
    audit = audit_settlement_snapshot(fixture)
    assert audit["status"] == "inconsistent"


def test_audit_collects_errors_from_multiple_bad_rows_without_crashing():
    fixture = load_fixture()
    fixture["items"][0]["line_total"] = "23.001"  # fractional cents
    fixture["items"][1]["quantity"] = "two"        # non-integer quantity
    audit = audit_settlement_snapshot(fixture)
    assert audit["status"] == "inconsistent"
    assert len(audit["errors"]) >= 2
    with pytest.raises(SettlementError):
        SettlementLedger.from_fixture(fixture)


# --------------------------------------------------------------------------- #
# Recompute invariants directly
# --------------------------------------------------------------------------- #
def test_recompute_full_return_allocates_every_cent_exactly_once():
    fixture = canonical_order()
    ledger = SettlementLedger.from_fixture(fixture)
    snap = ledger.snapshots["o-100"]
    active = {"i-100a": [0], "i-100b": [0]}
    cumulative = recompute_cumulative(snap, active, RETAIN_PROMOTION)
    assert cumulative["promotion_returned"] == 500
    assert cumulative["merchandise"] == 6000
    assert cumulative["tax"] == 480
    assert cumulative["total"] == snap.captured_total
    empty = recompute_cumulative(
        snap, {line.item_id: [] for line in snap.lines}, RETAIN_PROMOTION)
    assert empty["total"] == 0


def test_any_legal_return_order_converges_on_identical_cumulative_totals():
    import itertools
    base = [
        {"order_id": "o-100", "item_id": "i-100a", "quantity": 1,
         "promotion_contract": RETAIN_PROMOTION},
        {"order_id": "o-100", "item_id": "i-100b", "quantity": 1,
         "promotion_contract": RETAIN_PROMOTION},
        {"order_id": "o-200", "item_id": "i-200", "quantity": 1,
         "promotion_contract": RETAIN_PROMOTION},
        {"order_id": "o-200", "item_id": "i-200", "quantity": 1,
         "promotion_contract": RETAIN_PROMOTION},
        {"order_id": "o-200", "item_id": "i-200", "quantity": 1,
         "promotion_contract": RETAIN_PROMOTION},
    ]
    outcomes = set()
    for permutation in itertools.permutations(range(len(base))):
        requests = []
        for new_index, old_index in enumerate(permutation):
            payload = dict(base[old_index])
            payload["request_id"] = f"q-{new_index}"
            payload["route"] = "cash"
            requests.append(payload)
        result = settle(requests)
        summaries = tuple(
            (o["order_id"], o["cumulative_payable"],
             json.dumps(o["instrument_remaining"], sort_keys=True))
            for o in sorted(result["orders"], key=lambda o: o["order_id"])
        )
        outcomes.add(summaries)
    assert len(outcomes) == 1


def test_revoking_a_rejected_request_is_rejected_not_a_crash():
    result = settle([
        {"request_id": "q-bad", "order_id": "o-700", "item_id": "i-700",
         "quantity": 1, "route": "cash"},
        {"request_id": "q-r", "order_id": "o-700", "op": "revoke",
         "target_request_id": "q-bad"},
    ])
    decisions = decisions_by_id(result)
    assert decisions["q-bad"]["reason"] == "unpaid_order"
    assert decisions["q-r"]["reason"] == "revocation_target_inactive"


def test_malformed_money_and_quantity_are_reported_as_audit_errors():
    fixture = load_fixture()
    fixture["items"][0]["line_total"] = "23.081"
    with pytest.raises(SettlementError, match="invalid money|whole cents"):
        SettlementLedger.from_fixture(fixture)
    fixture = load_fixture()
    fixture["requests"] = [
        {"request_id": "q-bad", "order_id": "o-100", "item_id": "i-100a",
         "quantity": 1.5, "route": "cash"}]
    with pytest.raises(SettlementError, match="whole integer"):
        settle_fixture(fixture)


# --------------------------------------------------------------------------- #
# Legacy contract output must remain byte-for-byte compatible
# --------------------------------------------------------------------------- #
def test_legacy_intake_contract_is_unchanged():
    legacy = json.loads(
        (ROOT / "fixtures" / "returns" / "intake.json").read_text())
    decisions = assess_returns(
        legacy["orders"], legacy["items"], legacy["claims"])
    assert [d["status"] for d in decisions] == [
        "accepted", "accepted", "rejected", "accepted", "rejected"]
    assert [d["refund_cap"] for d in decisions] == [
        "25.00", "25.00", None, "40.00", None]
