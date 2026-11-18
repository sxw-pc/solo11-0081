"""Isolated tests for the combinatorial ad-slot auction and its two settlement contracts."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from synthlab import ad_auction
from synthlab.ad_auction import (
    TIE_BREAK_RULES,
    Allocation,
    AuctionError,
    Bid,
    CatalogView,
    build_certificate,
    catalog_from_spec,
    externality_payment,
    optimal_allocation,
    validate_intake,
)
from synthlab.ad_auction_intake import atomic_preview
from synthlab.ad_auction_intake import main as intake_main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "fixtures" / "ad_auction" / "intake.json"
DEMO_YAML = ROOT / "fixtures" / "demo" / "dataset.yaml"
FIXTURE_DATA = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

TINY_CATALOG = CatalogView(
    dataset="tiny_shop",
    blueprint_sha256="0" * 64,
    products=(("SKU-0001", "Widget", 9.99), ("SKU-0002", "Gadget", 19.99)),
)


def fixture_intake(**overrides):
    data = copy.deepcopy(FIXTURE_DATA)
    data.update(overrides)
    return data


def certify(data, catalog=TINY_CATALOG):
    return build_certificate(data, catalog=catalog)


class TestAllocation:
    def test_bundle_beats_atomic_preview(self):
        cert = certify(fixture_intake())
        assert cert["valid"] is True
        assert cert["allocation"]["winners"] == ["b1", "b4"]
        assert cert["allocation"]["total_value"] == 25
        assert cert["allocation"]["advertiser_count"] == 2
        # the legacy preview misses bundle b1 and stays at 24
        assert atomic_preview(FIXTURE_DATA) == ["b6", "b4", "b3"]
        assert cert["preview_baseline"] == {
            "winners": ["b6", "b4", "b3"],
            "total_value": 24,
            "optimal_gain": 1,
        }

    def test_same_advertiser_alternatives_never_co_win(self):
        cert = certify(fixture_intake())
        allocation = cert["allocation"]
        # b1 and b6 belong to adv-1; exactly one may win
        assert allocation["advertiser_winners"] == {"adv-1": "b1", "adv-4": "b4"}
        assert cert["auction"]["advertiser_bids"]["adv-1"] == ["b1", "b6"]
        rejected = {entry["bid_id"]: entry for entry in allocation["rejected_bids"]}
        assert rejected["b6"]["reason"] == "advertiser_mutex"
        assert "b1" in rejected["b6"]["detail"]
        assert rejected["b2"]["reason"] == "slot_conflict"
        assert rejected["b3"]["reason"] == "slot_conflict"

    def test_slot_conflict_goes_to_higher_value(self):
        data = fixture_intake(
            bids=[
                {"bid_id": "b1", "advertiser": "adv-1", "slots": ["hero"], "value": 10},
                {"bid_id": "b2", "advertiser": "adv-2", "slots": ["hero"], "value": 9},
            ]
        )
        cert = certify(data)
        assert cert["allocation"]["winners"] == ["b1"]
        assert cert["allocation"]["slot_winners"]["hero"] == "b1"
        rejected = cert["allocation"]["rejected_bids"]
        assert [entry["bid_id"] for entry in rejected] == ["b2"]
        assert rejected[0]["reason"] == "slot_conflict"

    def test_alternative_bids_single_advertiser(self):
        # both alternatives would fit side by side; at most one may win
        data = fixture_intake(
            bids=[
                {"bid_id": "b1", "advertiser": "adv-1", "slots": ["hero"], "value": 10},
                {"bid_id": "b2", "advertiser": "adv-1", "slots": ["sidebar"], "value": 8},
            ]
        )
        cert = certify(data)
        assert cert["allocation"]["winners"] == ["b1"]
        assert cert["allocation"]["rejected_bids"][0]["reason"] == "advertiser_mutex"

    def test_optimal_allocation_direct_bids(self):
        bids = [
            Bid("b1", "adv-1", ("hero", "sidebar"), 20),
            Bid("b2", "adv-2", ("hero",), 15),
        ]
        allocation = optimal_allocation(bids)
        assert allocation.winner_ids == ("b1",)
        assert allocation.total_value == 20

    def test_feasible_allocation_count_is_auditable(self):
        cert = certify(fixture_intake())
        # empty set + 5 singles + 6 legal pairs + 2 legal triples
        assert cert["allocation"]["feasible_allocations"] == 14


class TestTieBreaks:
    def test_equal_value_prefers_fewer_advertisers(self):
        data = fixture_intake(
            bids=[
                {"bid_id": "b1", "advertiser": "adv-1", "slots": ["hero", "sidebar"], "value": 10},
                {"bid_id": "b2", "advertiser": "adv-2", "slots": ["hero"], "value": 6},
                {"bid_id": "b3", "advertiser": "adv-3", "slots": ["sidebar"], "value": 4},
            ]
        )
        cert = certify(data)
        assert cert["allocation"]["winners"] == ["b1"]
        assert cert["allocation"]["total_value"] == 10
        assert cert["allocation"]["advertiser_count"] == 1

    def test_equal_value_and_advertisers_prefers_lower_bid_id(self):
        bids = [
            {"bid_id": "b2", "advertiser": "adv-2", "slots": ["hero"], "value": 5},
            {"bid_id": "b1", "advertiser": "adv-1", "slots": ["hero"], "value": 5},
        ]
        assert certify(fixture_intake(bids=bids))["allocation"]["winners"] == ["b1"]
        # reversed input order yields the same winner
        assert certify(fixture_intake(bids=list(reversed(bids))))["allocation"]["winners"] == ["b1"]


class TestNoLegalBids:
    def test_empty_bid_list_yields_empty_settlement(self):
        cert = certify(fixture_intake(bids=[]))
        assert cert["valid"] is True
        assert cert["allocation"]["winners"] == []
        assert cert["allocation"]["total_value"] == 0
        assert cert["settlements"]["first_price"]["seller_revenue"] == 0
        assert cert["settlements"]["externality_price"]["seller_revenue"] == 0

    def test_zero_value_bids_lose_to_empty_allocation(self):
        data = fixture_intake(
            bids=[{"bid_id": "b1", "advertiser": "adv-1", "slots": ["hero"], "value": 0}]
        )
        cert = certify(data)
        assert cert["allocation"]["winners"] == []
        assert cert["allocation"]["rejected_bids"][0]["reason"] == "tie_break"

    def test_all_bids_invalid_rejects_intake(self):
        data = fixture_intake(
            bids=[
                {"bid_id": "b1", "advertiser": "adv-1", "slots": ["banner"], "value": 1},
                {"bid_id": "b2", "advertiser": "adv-2", "slots": ["banner"], "value": 2},
            ]
        )
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert len(excinfo.value.issues) == 2
        assert {issue.code for issue in excinfo.value.issues} == {"AUC003"}


class TestFirstPrice:
    def test_fixture_payments_and_revenue(self):
        cert = certify(fixture_intake())
        first_price = cert["settlements"]["first_price"]
        assert first_price["per_bid"] == [
            {"bid_id": "b1", "advertiser": "adv-1", "value": 20, "payment": 20},
            {"bid_id": "b4", "advertiser": "adv-4", "value": 5, "payment": 5},
        ]
        assert first_price["invoices"] == [
            {"advertiser": "adv-1", "payment": 20},
            {"advertiser": "adv-4", "payment": 5},
        ]
        assert first_price["seller_revenue"] == 25


class TestExternalityPrice:
    def test_fixture_payments_and_revenue(self):
        cert = certify(fixture_intake())
        externality = cert["settlements"]["externality_price"]
        invoices = {entry["advertiser"]: entry for entry in externality["invoices"]}
        adv1 = invoices["adv-1"]
        # every adv-1 alternative is removed, not just the winning b1
        assert adv1["removed_bids"] == ["b1", "b6"]
        assert adv1["counterfactual"] == {"winners": ["b2", "b3", "b4"], "total_value": 23}
        assert adv1["others_original_value"] == 5
        assert adv1["payment"] == 18
        adv4 = invoices["adv-4"]
        assert adv4["removed_bids"] == ["b4"]
        assert adv4["counterfactual"] == {"winners": ["b1"], "total_value": 20}
        assert adv4["others_original_value"] == 20
        assert adv4["payment"] == 0
        assert externality["seller_revenue"] == 18

    def test_counterfactual_trap_single_bid_removal_overcharges(self):
        """Removing only the winning bid b1 -- and forgetting adv-1's other bid
        b6 -- would leave b6+b3+b4 = 24 feasible and overcharge adv-1 at 19
        instead of the correct 18."""
        intake = validate_intake(fixture_intake(), catalog_skus=TINY_CATALOG.skus)
        allocation = optimal_allocation(intake.bids)
        assert allocation.winner_ids == ("b1", "b4")
        # the buggy counterfactual: drop only the winning bid
        buggy = optimal_allocation([bid for bid in intake.bids if bid.bid_id != "b1"])
        assert buggy.winner_ids == ("b3", "b4", "b6")
        assert buggy.total_value == 24
        buggy_payment = externality_payment(buggy.total_value, 5)
        assert buggy_payment == 19
        # the certificate removes all of adv-1's bids and charges 18
        cert = certify(fixture_intake())
        invoices = cert["settlements"]["externality_price"]["invoices"]
        payment = next(entry["payment"] for entry in invoices if entry["advertiser"] == "adv-1")
        assert payment == 18 != buggy_payment


class TestNegativePrice:
    def test_negative_payment_rejected_not_clamped(self):
        assert externality_payment(23, 5) == 18
        assert externality_payment(20, 20) == 0
        with pytest.raises(AuctionError) as excinfo:
            externality_payment(3, 5)
        assert excinfo.value.code == "AUC301"

    def test_negative_payment_invalidates_certificate(self, monkeypatch):
        real = ad_auction.optimal_allocation

        def rigged(bids):
            allocation = real(bids)
            if len(bids) < 5:  # counterfactual markets suddenly worth nothing
                return Allocation(allocation.winners, 0, allocation.feasible_count)
            return allocation

        monkeypatch.setattr(ad_auction, "optimal_allocation", rigged)
        cert = certify(fixture_intake())
        assert cert["valid"] is False
        assert cert["invalid_reasons"]
        assert cert["settlements"]["externality_price"]["valid"] is False
        # the independent first-price contract is unaffected
        assert cert["settlements"]["first_price"]["seller_revenue"] == 25


class TestValidation:
    def test_unknown_sku_rejected(self):
        with pytest.raises(AuctionError) as excinfo:
            certify(fixture_intake(sku_id="SKU-9999"))
        assert excinfo.value.code == "AUC002"

    def test_negative_value_rejected(self):
        data = fixture_intake()
        data["bids"][0]["value"] = -1
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC006" in {issue.code for issue in excinfo.value.issues}

    def test_duplicate_bid_id_rejected(self):
        data = fixture_intake()
        data["bids"][1]["bid_id"] = "b1"
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC004" in {issue.code for issue in excinfo.value.issues}

    def test_duplicate_slot_request_rejected(self):
        data = fixture_intake()
        data["bids"][1]["slots"] = ["hero", "hero"]
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC005" in {issue.code for issue in excinfo.value.issues}

    def test_unknown_slot_rejected(self):
        data = fixture_intake()
        data["bids"][0]["slots"] = ["hero", "banner"]
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC003" in {issue.code for issue in excinfo.value.issues}

    def test_bid_limit_enforced(self):
        data = fixture_intake()
        data["bids"] += [
            {"bid_id": f"b{n}", "advertiser": f"adv-{n}", "slots": ["footer"], "value": 1}
            for n in range(10, 14)
        ]
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC007" in {issue.code for issue in excinfo.value.issues}

    def test_unknown_contract_rejected(self):
        data = fixture_intake()
        data["contracts"]["magic_price"] = {"payment_rule": "winning_bid"}
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC009" in {issue.code for issue in excinfo.value.issues}

    def test_payment_rule_mismatch_rejected(self):
        data = fixture_intake()
        data["contracts"]["first_price"] = {"payment_rule": "vcg_externality"}
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC009" in {issue.code for issue in excinfo.value.issues}

    def test_empty_advertiser_rejected(self):
        data = fixture_intake()
        data["bids"][0]["advertiser"] = ""
        with pytest.raises(AuctionError) as excinfo:
            certify(data)
        assert "AUC008" in {issue.code for issue in excinfo.value.issues}


class TestInputReordering:
    def test_bid_order_does_not_change_certificate(self):
        baseline = certify(fixture_intake())
        reordered = fixture_intake(bids=list(reversed(copy.deepcopy(FIXTURE_DATA["bids"]))))
        assert certify(reordered) == baseline

    def test_slot_order_does_not_change_outcome(self):
        baseline = certify(fixture_intake())
        cert = certify(fixture_intake(slots=["footer", "sidebar", "hero"]))
        assert cert["allocation"] == baseline["allocation"]
        assert cert["settlements"] == baseline["settlements"]


class TestCatalogIdentity:
    def test_engine_catalog_binds_eligibility(self, demo_spec, demo_data):
        catalog = catalog_from_spec(demo_spec)
        assert "SKU-0001" in catalog.skus
        # regeneration from the same blueprint is deterministic
        assert catalog.fingerprint == CatalogView(
            demo_spec.name,
            demo_spec.blueprint_sha256,
            tuple(demo_data.tables["products"].rows),
        ).fingerprint
        cert = build_certificate(fixture_intake(), catalog=catalog)
        assert cert["valid"] is True
        assert cert["catalog"]["sku_verified"] is True
        assert cert["catalog"]["products_fingerprint"] == catalog.fingerprint

    def test_changed_product_identity_changes_eligibility(self, demo_spec, demo_data):
        catalog = catalog_from_spec(demo_spec)
        rows = demo_data.tables["products"].rows
        tampered = CatalogView(
            catalog.dataset,
            catalog.blueprint_sha256,
            tuple(("SKU-9000",) + tuple(row[1:]) for row in rows),
        )
        assert tampered.fingerprint != catalog.fingerprint
        with pytest.raises(AuctionError) as excinfo:
            build_certificate(fixture_intake(), catalog=tampered)
        assert excinfo.value.code == "AUC002"


class TestAllocationDrivesPayments:
    def test_modified_bids_recompute_both_contracts(self):
        data = fixture_intake()
        by_id = {bid["bid_id"]: bid for bid in data["bids"]}
        by_id["b2"]["value"] = 25  # single hero bid now beats the bundle
        cert = certify(data)
        assert cert["allocation"]["winners"] == ["b2", "b3", "b4"]
        first_price = cert["settlements"]["first_price"]
        assert [(entry["bid_id"], entry["payment"]) for entry in first_price["per_bid"]] == [
            ("b2", 25),
            ("b3", 3),
            ("b4", 5),
        ]
        assert first_price["seller_revenue"] == 33
        externality = cert["settlements"]["externality_price"]
        payments = {entry["advertiser"]: entry["payment"] for entry in externality["invoices"]}
        assert payments == {"adv-2": 17, "adv-3": 0, "adv-4": 0}
        assert externality["seller_revenue"] == 17
        # neither contract reused the fixture's 25/18 figures
        assert first_price["seller_revenue"] != 25
        assert externality["seller_revenue"] != 18


class TestCertificateReview:
    def test_certificate_is_self_describing(self):
        cert = certify(fixture_intake())
        assert cert["auction"]["tie_breaks"] == list(TIE_BREAK_RULES)
        assert cert["auction"]["limits"] == {"max_bids": 8, "max_slots": 4}
        assert cert["allocation"]["slot_winners"] == {
            "hero": "b1",
            "sidebar": "b1",
            "footer": "b4",
        }
        reasons = {entry["bid_id"]: entry["reason"] for entry in cert["allocation"]["rejected_bids"]}
        assert reasons == {"b2": "slot_conflict", "b3": "slot_conflict", "b6": "advertiser_mutex"}
        # offline recomputation: the certificate is JSON round-trippable
        assert json.loads(json.dumps(cert)) == cert


class TestCli:
    def test_main_emits_certificate(self, capsys):
        rc = intake_main(["--fixture", str(FIXTURE_PATH), "--dataset", str(DEMO_YAML)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["valid"] is True
        assert out["allocation"]["winners"] == ["b1", "b4"]
        assert out["catalog"]["sku_id"] == "SKU-0001"
        assert out["catalog"]["sku_verified"] is True
        assert out["fixture"]["sha256"] == hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
        assert out["settlements"]["first_price"]["seller_revenue"] == 25
        assert out["settlements"]["externality_price"]["seller_revenue"] == 18

    def test_main_rejects_invalid_fixture(self, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(fixture_intake(sku_id="SKU-9999")), encoding="utf-8")
        rc = intake_main(["--fixture", str(path), "--dataset", str(DEMO_YAML)])
        assert rc == 2
        err = json.loads(capsys.readouterr().err)
        assert err["valid"] is False
        assert err["errors"][0]["code"] == "AUC002"

    def test_atomic_preview_flag_keeps_legacy_output(self, capsys):
        rc = intake_main(["--fixture", str(FIXTURE_PATH), "--atomic-preview"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == ["b6", "b4", "b3"]
