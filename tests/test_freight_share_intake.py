import json
import os
import subprocess
import sys
from copy import deepcopy
from itertools import permutations
from pathlib import Path

import pytest

from synthlab.engine import generate_dataset
from synthlab.freight_share_intake import build_report, solo_quotes
from synthlab.freight_sharing import (
    ContractRejectedError,
    FreightInfeasibleError,
    FreightValidationError,
    marginal_shares,
    settle,
    solve_fleet,
)
from synthlab.spec.loader import load_dataset

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "freight_share" / "intake.json"
DEMO_YAML = ROOT / "fixtures" / "demo" / "dataset.yaml"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def coalition_map(report):
    # Sort keys because coalition listing order follows the input merchants.
    return {tuple(sorted(c["coalition"])): c["cost"] for c in report["coalition_costs"]}


def invoices(report, contract):
    return {
        invoice["merchant_id"]: invoice
        for invoice in report["contracts"][contract]["invoices"]
    }


# --------------------------------------------------------------------------- #
# Existing behavior: catalog identity and the 54 -> 37 counter-example
# --------------------------------------------------------------------------- #

def test_shipments_reference_generated_skus():
    data = load_fixture()
    rows = generate_dataset(load_dataset(DEMO_YAML)).tables["products"].rows
    assert {m["sku_id"] for m in data["merchants"]} <= {r[0] for r in rows}


def test_solo_quotes_miss_shared_vehicle_savings():
    data = load_fixture()
    assert solo_quotes(data) == {"m-A": 20, "m-B": 18, "m-C": 16}
    assert sum(solo_quotes(data).values()) == 54
    dist = data["distance"]
    ab = dist["D"]["A"] + dist["A"]["B"] + dist["B"]["D"]
    c = dist["D"]["C"] + dist["C"]["D"]
    assert ab + c == 37


def test_shared_routing_recovers_37_not_naive_54_split():
    report = settle(load_fixture())
    assert report["solo_total"] == 54
    assert report["grand_coalition"]["cost"] == 37
    # A naive 54/3 split would bill m-C 18, breaching its solo upper bound of 16.
    weight = invoices(report, "weight_share")
    assert weight["m-C"]["amount_cents"] <= 1600
    # Every leg of every trip is an actual directed edge of the network.
    for trip in report["grand_coalition"]["trips"]:
        assert trip["stops"][0] == "D" and trip["stops"][-1] == "D"
        assert sum(leg["distance"] for leg in trip["legs"]) == trip["distance"]
        assert trip["units_loaded"] <= trip["vehicle_capacity"]


# --------------------------------------------------------------------------- #
# Exact coalition costs, per-trip evidence and fresh sub-coalition routing
# --------------------------------------------------------------------------- #

EXPECTED_COSTS = {
    (): 0,
    ("m-A",): 20,
    ("m-B",): 18,
    ("m-C",): 16,
    ("m-A", "m-B"): 21,
    ("m-A", "m-C"): 23,
    ("m-B", "m-C"): 23,
    ("m-A", "m-B", "m-C"): 37,
}


def test_all_coalition_costs_match_exact_search_with_evidence():
    report = settle(load_fixture())
    costs = coalition_map(report)
    assert costs == EXPECTED_COSTS
    # Each coalition's evidence is a legal partition that really costs c(S).
    for entry in report["coalition_costs"]:
        covered = []
        total = 0
        for trip in entry["trips"]:
            assert trip["stops"][0] == "D" and trip["stops"][-1] == "D"
            assert trip["units_loaded"] <= trip["vehicle_capacity"]
            assert sum(leg["distance"] for leg in trip["legs"]) == trip["distance"]
            covered.extend(trip["merchants"])
            total += trip["distance"]
        assert sorted(covered) == sorted(entry["coalition"])
        assert total == entry["cost"]
    grand = report["grand_coalition"]
    assert [trip["merchants"] for trip in grand["trips"]] == [["m-A", "m-B"], ["m-C"]]
    assert grand["trips"][0]["stops"] == ["D", "A", "B", "D"]
    assert grand["trips"][1]["stops"] == ["D", "C", "D"]


def test_subcoalitions_are_routed_independently_not_reusing_grand_route():
    # Grand coalition runs A+B together (21) plus C (16). If the grand route
    # were reused for coalition {A, C}, it would pay the A/B trip's mileage
    # without B present; the exact sub-coalition optimum is instead 23.
    report = settle(load_fixture())
    costs = coalition_map(report)
    ac = next(c for c in report["coalition_costs"] if tuple(c["coalition"]) == ("m-A", "m-C"))
    assert costs[("m-A", "m-C")] == 23
    assert len(ac["trips"]) == 1
    assert ac["trips"][0]["merchants"] == ["m-A", "m-C"]
    assert ac["trips"][0]["distance"] == 23
    # Recompute every coalition with a brand-new DP solve and demand equality.
    data = load_fixture()
    shipments_ids = [m["merchant_id"] for m in data["merchants"]]
    from synthlab.freight_sharing import parse_intake

    depot, capacity, shipments = parse_intake(data)
    dp, _chosen, _options = solve_fleet(shipments, depot, capacity, data["distance"])
    for mask in range(1 << len(shipments)):
        members = tuple(
            sorted(shipments_ids[idx] for idx in range(len(shipments)) if mask & (1 << idx))
        )
        assert dp[mask] == costs[members]


def test_direction_asymmetric_edges_change_optimal_visit_order():
    data = load_fixture()
    # Break the A<->B symmetry: A->B stays cheap, B->A becomes expensive.
    data["distance"]["B"]["A"] = 30
    report = settle(data)
    ab = next(
        c for c in report["coalition_costs"]
        if tuple(c["coalition"]) == ("m-A", "m-B")
    )
    # D->B->A->D = 9 + 30 + 10 = 49 vs D->A->B->D = 10 + 2 + 9 = 21.
    assert ab["cost"] == 21
    assert ab["trips"][0]["stops"] == ["D", "A", "B", "D"]
    # The reverse asymmetry must pick the reverse order:
    # D->B->A->D = 9 + 30 + 10 = 49 vs D->A->B->D = 10 + 30 + 9 = 49... so use
    # a one-sided edge change that makes only the reverse order competitive.
    other = load_fixture()
    other["distance"]["A"]["B"] = 30
    other["distance"]["B"]["A"] = 1
    report_other = settle(other)
    ab_other = next(
        c for c in report_other["coalition_costs"]
        if tuple(sorted(c["coalition"])) == ("m-A", "m-B")
    )
    # D->B->A->D = 9 + 1 + 10 = 20 vs D->A->B->D = 10 + 30 + 9 = 49.
    assert ab_other["cost"] == 20
    assert ab_other["trips"][0]["stops"] == ["D", "B", "A", "D"]


def test_shared_route_does_not_double_count_capacity():
    data = load_fixture()
    data["vehicle_capacity"] = 2
    report = settle(data)
    shared_trip = next(
        trip
        for trip in report["grand_coalition"]["trips"]
        if trip["merchants"] == ["m-A", "m-B"]
    )
    # Two distinct shipments share one vehicle but each is delivered once:
    # loaded units is 2, not 4, and capacity is never exceeded.
    assert shared_trip["units_loaded"] == 2
    assert shared_trip["units_loaded"] <= shared_trip["vehicle_capacity"]
    assert len(shared_trip["merchants"]) == len(set(shared_trip["merchants"])) == 2
    # Tightening capacity to 1 forbids every shared trip; costs revert to solos.
    data["vehicle_capacity"] = 1
    solo_report = settle(data)
    costs = coalition_map(solo_report)
    assert costs[("m-A", "m-B")] == 20 + 18
    assert costs[("m-A", "m-B", "m-C")] == 54
    grand_trips = solo_report["grand_coalition"]["trips"]
    assert all(len(trip["merchants"]) == 1 for trip in grand_trips)


def test_two_trips_pay_shared_mileage_twice_not_once():
    data = load_fixture()
    data["vehicle_capacity"] = 1
    report = settle(data)
    # Every merchant must take its own trip; shared D-adjacent legs are driven
    # separately per trip, so the total is the sum of solo round trips (54),
    # never a single merged mileage count.
    assert report["grand_coalition"]["cost"] == 54
    assert len(report["grand_coalition"]["trips"]) == 3
    assert sum(trip["distance"] for trip in report["grand_coalition"]["trips"]) == 54


# --------------------------------------------------------------------------- #
# Contracts: exact invoices, cent conservation and solo upper bounds
# --------------------------------------------------------------------------- #

def test_weight_share_invoices_and_cent_remainder():
    report = settle(load_fixture())
    contract = report["contracts"]["weight_share"]
    assert contract["status"] == "issued"
    weight = invoices(report, "weight_share")
    assert weight["m-A"]["amount"] == "12.34"
    assert weight["m-B"]["amount"] == "12.33"
    assert weight["m-C"]["amount"] == "12.33"
    cents = [weight[m]["amount_cents"] for m in ("m-A", "m-B", "m-C")]
    assert sum(cents) == 3700  # strictly equal to c(grand) in cents
    assert all(invoice["within_solo_bound"] for invoice in weight.values())


def test_marginal_share_is_full_shapley_average_not_single_delete_delta():
    report = settle(load_fixture())
    contract = report["contracts"]["marginal_share"]
    assert contract["status"] == "issued"
    marginal = invoices(report, "marginal_share")
    assert marginal["m-A"]["amount"] == "13.00"
    assert marginal["m-B"]["amount"] == "12.00"
    assert marginal["m-C"]["amount"] == "12.00"
    assert sum(invoice["amount_cents"] for invoice in marginal.values()) == 3700
    # Each merchant has exactly 2^(n-1) marginal terms covering all subsets,
    # and the permutation-weighted terms reproduce its Shapley invoice.
    for merchant_id, terms in contract["marginal_terms"].items():
        assert len(terms) == 4
        from fractions import Fraction

        weighted = sum(
            Fraction(term["permutation_weight"]) * term["marginal_cost"]
            for term in terms
        )
        assert float(weighted) == marginal[merchant_id]["amount_cents"] / 100
        for term in terms:
            assert term["marginal_cost"] == term["cost_with"] - term["cost_without"]
            assert term["cost_with"] >= term["cost_without"]
    # A single "delete from grand coalition" delta would give A: 37-23=14,
    # B: 37-23=14, C: 37-21=16 -- none of which match the Shapley invoices.
    assert marginal["m-A"]["amount_cents"] != 1400
    assert marginal["m-B"]["amount_cents"] != 1400
    assert marginal["m-C"]["amount_cents"] != 1600


def test_marginal_math_recomputes_from_subcoalition_cost_table():
    data = load_fixture()
    from synthlab.freight_sharing import parse_intake

    depot, capacity, shipments = parse_intake(data)
    dp, _chosen, _options = solve_fleet(shipments, depot, capacity, data["distance"])
    shares = marginal_shares(shipments, dp)
    # 13/12/12 on the fixture cost game, as exact rationals.
    assert [float(value) for value in shares] == [13.0, 12.0, 12.0]
    # Shapley efficiency: shares add up exactly to the grand coalition cost.
    assert sum(shares) == 37


def test_invoices_are_non_negative_and_within_solo_upper_bounds():
    report = settle(load_fixture())
    for contract_name, contract in report["contracts"].items():
        assert contract["status"] == "issued"
        for invoice in contract["invoices"]:
            assert invoice["amount_cents"] >= 0
            assert invoice["amount_cents"] <= invoice["solo_upper_bound_cents"]
            assert invoice["solo_upper_bound_cents"] == (
                report["solo_quotes"][invoice["merchant_id"]] * 100
            )
        assert sum(i["amount_cents"] for i in contract["invoices"]) == contract["total_cents"]
        assert contract["total_cents"] == report["grand_coalition"]["cost"] * 100


def test_weight_contract_rejected_when_rule_breaches_solo_bound():
    # Two units for m-C against one unit each for A and B: the proportional rule
    # charges C half of 37 = 18.50, above C's solo upper bound of 16. The
    # contract must be rejected (no silent truncation), conservation preserved.
    data = load_fixture()
    data["merchants"][2]["units"] = 2
    data["vehicle_capacity"] = 3
    report = settle(data)
    weight = report["contracts"]["weight_share"]
    marginal = report["contracts"]["marginal_share"]
    assert weight["status"] == "rejected"
    assert "m-C" in " ".join(weight["reasons"])
    assert "invoices" not in weight  # no truncated invoice set is issued
    assert weight["total_cents"] == 3700
    # The Shapley contract is unit-independent here and still issues correctly.
    assert marginal["status"] == "issued"
    assert sum(i["amount_cents"] for i in marginal["invoices"]) == 3700


def test_cent_allocation_is_stable_under_input_reordering():
    base = load_fixture()
    reference = settle(base)
    ref_weight = {
        i["merchant_id"]: i["amount_cents"]
        for i in reference["contracts"]["weight_share"]["invoices"]
    }
    for ordering in permutations(range(len(base["merchants"]))):
        shuffled = deepcopy(base)
        shuffled["merchants"] = [base["merchants"][i] for i in ordering]
        report = settle(shuffled)
        weight = invoices(report, "weight_share")
        marginal = invoices(report, "marginal_share")
        for merchant_id, cents in ref_weight.items():
            assert weight[merchant_id]["amount_cents"] == cents
            assert weight[merchant_id]["amount_cents"] == ref_weight[merchant_id]
        # Coalition costs are order-independent too.
        costs = coalition_map(report)
        assert costs[("m-A", "m-B", "m-C")] == 37
        assert costs[("m-A", "m-B")] == 21
        # Marginal invoices stay 13/12/12 regardless of input order.
        assert marginal["m-A"]["amount_cents"] == 1300
        assert marginal["m-B"]["amount_cents"] == 1200
        assert marginal["m-C"]["amount_cents"] == 1200


# --------------------------------------------------------------------------- #
# Validation and feasibility rejections
# --------------------------------------------------------------------------- #

def test_unknown_sku_rejected_with_catalog_present():
    data = load_fixture()
    valid_skus = {m["sku_id"] for m in data["merchants"]}
    settle(data, valid_skus=valid_skus)  # accepted with the matching catalog
    bad = deepcopy(data)
    bad["merchants"][0]["sku_id"] = "SKU-9999"
    with pytest.raises(FreightValidationError, match="unknown sku_id"):
        settle(bad, valid_skus=valid_skus)
    # An embedded catalog in the document is honored the same way.
    embedded = deepcopy(bad)
    embedded["catalog"] = sorted(valid_skus)
    with pytest.raises(FreightValidationError, match="unknown sku_id"):
        settle(embedded)


def test_duplicate_merchant_id_rejected():
    data = load_fixture()
    data["merchants"][1]["merchant_id"] = "m-A"
    with pytest.raises(FreightValidationError, match="duplicate merchant_id"):
        settle(data)


def test_negative_distance_and_negative_or_zero_units_rejected():
    data = load_fixture()
    data["distance"]["A"]["B"] = -2
    with pytest.raises(FreightValidationError, match="non-negative integer"):
        settle(data)

    for bad_units in (0, -1):
        bad = load_fixture()
        bad["merchants"][0]["units"] = bad_units
        with pytest.raises(FreightValidationError, match="units must be a positive integer"):
            settle(bad)


def test_missing_directed_edge_rejected_and_direction_matters():
    data = load_fixture()
    del data["distance"]["B"]["D"]
    with pytest.raises(FreightValidationError, match="missing directed distance edge"):
        settle(data)
    # One direction present does not imply the reverse.
    other = load_fixture()
    del other["distance"]["D"]["A"]
    with pytest.raises(FreightValidationError, match="missing directed distance edge"):
        settle(other)


def test_infeasible_shipment_exceeding_capacity_never_quoted_zero():
    data = load_fixture()
    data["merchants"][0]["units"] = 5
    data["vehicle_capacity"] = 3
    with pytest.raises(FreightInfeasibleError, match="no feasible shared-freight routing"):
        settle(data)


def test_over_six_merchants_rejected_and_empty_shipments_rejected():
    data = load_fixture()
    data["merchants"] = [
        {
            "merchant_id": f"m-{idx}",
            "stop": chr(ord("A") + idx),
            "sku_id": f"SKU-{idx:04d}",
            "units": 1,
        }
        for idx in range(7)
    ]
    with pytest.raises(FreightValidationError, match="at most 6 merchants"):
        settle(data)
    empty = load_fixture()
    empty["merchants"] = []
    with pytest.raises(FreightValidationError, match="non-empty list"):
        settle(empty)


def test_malformed_document_shapes_are_rejected():
    with pytest.raises(FreightValidationError):
        settle({"depot": "D"})  # no merchants/distance
    bad_capacity = load_fixture()
    bad_capacity["vehicle_capacity"] = 0
    with pytest.raises(FreightValidationError, match="vehicle_capacity"):
        settle(bad_capacity)
    bad_rule = load_fixture()
    bad_rule["contracts"]["weight_share"]["charge_rule"] = "split_evenly"
    with pytest.raises(FreightValidationError, match="unsupported charge_rule"):
        settle(bad_rule)


def test_catalog_change_flows_through_to_rejection():
    # Merchandise identity is part of the validated batch: a SKU that the
    # engine catalog no longer contains blocks issuance before any routing.
    data = load_fixture()
    generated = generate_dataset(load_dataset(DEMO_YAML))
    catalog = {row[0] for row in generated.tables["products"].rows}
    assert settle(data, valid_skus=catalog)["grand_coalition"]["cost"] == 37
    stale = deepcopy(data)
    stale["merchants"][1]["sku_id"] = "SKU-0099"
    with pytest.raises(FreightValidationError, match="unknown sku_id"):
        settle(stale, valid_skus=catalog)


# --------------------------------------------------------------------------- #
# End-to-end entry point
# --------------------------------------------------------------------------- #

def test_cli_emits_full_report_and_zero_exit():
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "synthlab.freight_share_intake",
            "--fixture",
            str(FIXTURE),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["grand_coalition"]["cost"] == 37
    assert invoices(report, "weight_share")["m-A"]["amount"] == "12.34"
    assert invoices(report, "marginal_share")["m-A"]["amount"] == "13.00"


def test_cli_dataset_flag_validates_sku_identities():
    report = build_report(FIXTURE, DEMO_YAML)
    assert report["grand_coalition"]["cost"] == 37
    data = load_fixture()
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        stale_path = Path(tmp) / "stale.json"
        stale = deepcopy(data)
        stale["merchants"][0]["sku_id"] = "SKU-4040"
        stale_path.write_text(json.dumps(stale), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "synthlab.freight_share_intake",
                "--fixture",
                str(stale_path),
                "--dataset",
                str(DEMO_YAML),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        assert proc.returncode == 2
        assert "unknown sku_id" in proc.stderr
