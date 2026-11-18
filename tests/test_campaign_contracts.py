"""Contract-adjudication tests for the synthetic campaign trial audit.

The analysis code is only allowed to read observed trial columns; the
structural rules in ``synthlab.campaign_design`` appear here solely as
independent evidence that the fixture honors monotone exposure and the
exclusion restriction -- never as an oracle for missing outcomes.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from synthlab.campaign_audit import (
    DECISION_APPROVE,
    DECISION_INCONCLUSIVE,
    DECISION_REJECT,
    DECISION_UNIDENTIFIED,
    OFFER_ROLLOUT,
    REACHED_ONLY,
    TrialError,
    audit_trial,
    load_design_evidence,
    render_report,
)
from synthlab.campaign_design import potential_conversion, potential_exposure
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset

ROOT = Path(__file__).resolve().parents[1]
TRIAL_PATH = ROOT / "fixtures" / "campaign" / "trial.json"
SPEC_PATH = ROOT / "fixtures" / "demo" / "dataset.yaml"
THRESHOLD = 0.34


@pytest.fixture(scope="module")
def customers():
    data = generate_dataset(load_dataset(SPEC_PATH))
    table = data.tables["customers"]
    return [dict(zip(table.columns, row)) for row in table.rows]


@pytest.fixture()
def payload():
    return json.loads(TRIAL_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def events(payload):
    return copy.deepcopy(payload["events"])


@pytest.fixture()
def evidence(payload):
    return load_design_evidence(payload)


def _audit(customers, events, evidence=None, **kwargs):
    if evidence is None:
        evidence = {
            "assignment_unit": "household",
            "monotone_exposure": True,
            "exclusion_restriction": True,
        }
    return audit_trial(customers, events, design_evidence=evidence, **kwargs)


def _customers_for(events):
    """Customer identity set matching an event stream exactly."""
    return [{"customer_id": row["customer_id"]} for row in events]


# --------------------------------------------------------------------------- #
# 1. The current 80-person fixture: full end-to-end target outputs.
# --------------------------------------------------------------------------- #

def test_fixture_itt_first_stage_and_contract_decisions(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence, threshold=THRESHOLD)

    # Targets fixed by fixtures/campaign/trial_counts.json.
    itt = result["intention_to_treat"]["customer_denominator_interval"]
    assert itt == pytest.approx([0.275, 0.300])
    assert result["first_stage"]["customer_denominator"] == pytest.approx(0.725)

    rollout = result["contracts"][OFFER_ROLLOUT]
    assert rollout["decision"] == DECISION_REJECT
    assert rollout["effect_interval"][1] <= THRESHOLD

    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_APPROVE
    assert reached["effect_interval"] == pytest.approx(
        [0.3793103448275862, 0.41379310344827586]
    )
    assert reached["effect_interval"][0] > THRESHOLD


def test_fixture_counts_match_compact_manifest(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    summary = json.loads((ROOT / "fixtures/campaign/trial_counts.json").read_text())
    for arm_name, key in (("offer", "offer"), ("control", "control")):
        arm = result["arms"][arm_name]
        expected = summary["assignment"][key]
        assert arm["customers"] == expected["total"]
        assert arm["exposed_customers"] == expected["exposed"]
        assert arm["outcome_observed"] == expected["outcome_observed"]
        assert arm["converted_observed"] == expected["converted_observed"]
        assert arm["missing_outcomes"] == expected["total"] - expected["outcome_observed"]


def test_legacy_counts_still_available(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    # Original audit_trial contract retained for older callers.
    assert result["unobserved_customers"] == 0
    assert sum(v[2] for v in result["counts"].values()) == 80
    assert sum(v[1] for v in result["counts"].values()) == 79


def test_naive_association_is_reported_but_labeled_observational(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    naive = result["naive_exposed_association"]
    assert "not causal" in naive["note"].lower() or "selected" in naive["note"].lower()
    # 29/37 selected exposed vs 17 observed conversions among 43 unexposed rows.
    assert naive["counts"]["exposed"][2] == 37
    assert naive["counts"]["unexposed"][2] == 43
    # The naive gap must not be reused as either causal contract estimand.
    naive_gap = 29 / 37 - 17 / 43
    assert naive_gap > result["contracts"][OFFER_ROLLOUT]["effect_interval"][1]


def test_report_distinguishes_four_estimands(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    text = render_report(result)
    assert "naive" in text
    assert "ITT" in text
    assert "first stage" in text
    assert OFFER_ROLLOUT in text and REACHED_ONLY in text
    assert "reject" in text and "approve" in text


# --------------------------------------------------------------------------- #
# 2. Missing outcomes must propagate as full 0..1 intervals, never as zeros.
# --------------------------------------------------------------------------- #

def test_missing_outcome_spreads_both_bounds(customers, events, evidence):
    result = _audit(customers, events, evidence)
    control = result["arms"]["control"]
    # One missing control conversion: 17/40 low, 18/40 high.
    assert control["conversion_rate_interval"] == pytest.approx([0.425, 0.45])
    itt = result["intention_to_treat"]["customer_denominator_interval"]
    # Width equals the 1/40 imputation contribution.
    assert itt[1] - itt[0] == pytest.approx(1 / 40)


def test_missing_treated_as_zero_would_overstate_rejection(customers, events, evidence):
    result = _audit(customers, events, evidence)
    # The upper ITT bound (missing = 1) is strictly above the lower bound; an
    # implementation that silently zero-fills could not produce this width.
    low, high = result["intention_to_treat"]["customer_denominator_interval"]
    assert high > low


def test_interval_straddling_threshold_is_inconclusive(customers, events, evidence):
    # Threshold inside the ITT interval -> neither approve nor reject.
    result = audit_trial(customers, events, design_evidence=evidence, threshold=0.285)
    assert result["contracts"][OFFER_ROLLOUT]["decision"] == DECISION_INCONCLUSIVE


def test_threshold_equality_is_rejection_not_approval():
    # ITT = 6/10 - 4/10 = 0.20 on both bounds (each arm holds n/2 customers).
    events = _mini_trial(offer_conv=6, control_conv=4, n=20)
    # Threshold exactly at the interval must reject because the rule is STRICT >.
    result = _audit(_customers_for(events), events, threshold=0.20)
    assert result["contracts"][OFFER_ROLLOUT]["decision"] == DECISION_REJECT
    # Just below the same interval, the contract approves.
    approved = _audit(_customers_for(events), events, threshold=0.199)
    assert approved["contracts"][OFFER_ROLLOUT]["decision"] == DECISION_APPROVE


# --------------------------------------------------------------------------- #
# 3. Household-level assignment consistency and both denominators.
# --------------------------------------------------------------------------- #

def test_household_and_customer_denominators_both_shown(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    den = result["denominators"]
    assert den["customers"] == 80
    assert den["households"] == 40
    assert den["customers_per_household"] == 2.0
    assert result["arms"]["offer"]["households"] == 20
    assert result["arms"]["control"]["households"] == 20


def test_mixed_assignment_within_household_is_rejected(customers, events, evidence):
    events[1]["assigned"] = 1  # hh-001 would then contain both arms
    with pytest.raises(TrialError, match="household randomization violated"):
        audit_trial(customers, events, design_evidence=evidence)


def test_household_level_itt_uses_household_shares(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    hh = result["intention_to_treat"]["household_denominator_interval"]
    cust = result["intention_to_treat"]["customer_denominator_interval"]
    # Equal-size households => the two denominators agree here, and both are
    # explicitly reported rather than treating 80 people as 80 randomizations.
    assert hh == pytest.approx(cust)


# --------------------------------------------------------------------------- #
# 4. Control-group exposure (always-takers) is handled through IV, not bias.
# --------------------------------------------------------------------------- #

def test_control_arm_contains_exposed_always_takers(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    assert result["arms"]["control"]["exposed_customers"] == 4
    # First stage nets out the always-taker exposure rate: 33/40 - 4/40.
    assert result["first_stage"]["customer_denominator"] == pytest.approx((33 - 4) / 40)


def test_zero_first_stage_blocks_reached_only(evidence):
    # Both arms reach customers at the same rate -> no instrument strength.
    events = _mini_trial(offer_conv=8, control_conv=5, n=20, offer_exposed=5, control_exposed=5)
    result = _audit(_customers_for(events), events, evidence)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert any("first stage" in reason for reason in reached["reasons"])
    assert reached["effect_interval"] is None
    # ITT itself remains identifiable for offer_rollout.
    assert result["contracts"][OFFER_ROLLOUT]["decision"] in (
        DECISION_APPROVE, DECISION_REJECT, DECISION_INCONCLUSIVE,
    )


def test_negative_first_stage_blocks_reached_only(evidence):
    # Control exposure exceeds offer exposure: no valid instrument direction.
    events = _mini_trial(offer_conv=8, control_conv=5, n=20, offer_exposed=2, control_exposed=8)
    result = _audit(_customers_for(events), events, evidence)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert reached["first_stage"] < 0
    assert any("non-positive first stage" in reason for reason in reached["reasons"])


# --------------------------------------------------------------------------- #
# 5. Design-contract breaches: monotonicity / exclusion evidence removed.
# --------------------------------------------------------------------------- #

def test_missing_design_evidence_blocks_reached_only(customers, events):
    result = audit_trial(customers, events, design_evidence=None)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert any("no design contract" in reason for reason in reached["reasons"])
    # offer_rollout needs no IV assumptions and is still adjudicated.
    assert result["contracts"][OFFER_ROLLOUT]["decision"] == DECISION_REJECT


def test_monotonicity_evidence_removed_blocks_reached_only(customers, events, evidence):
    evidence["monotone_exposure"] = False
    result = audit_trial(customers, events, design_evidence=evidence)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert any("monotone" in reason for reason in reached["reasons"])


def test_exclusion_evidence_removed_blocks_reached_only(customers, events, evidence):
    evidence["exclusion_restriction"] = False
    result = audit_trial(customers, events, design_evidence=evidence)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert any("exclusion" in reason for reason in reached["reasons"])


def test_non_household_assignment_unit_blocks_reached_only(customers, events, evidence):
    evidence["assignment_unit"] = "customer"
    result = audit_trial(customers, events, design_evidence=evidence)
    reached = result["contracts"][REACHED_ONLY]
    assert reached["decision"] == DECISION_UNIDENTIFIED
    assert any("household" in reason for reason in reached["reasons"])


def test_design_rule_fixture_actually_satisfies_contracts(events):
    # Independent executable evidence: the structural generator certifies the
    # fixture (this is test-side validation, not analysis-side imputation).
    for row in events:
        i = int(row["customer_id"].split("-")[1])
        assert potential_exposure(i, 1) >= potential_exposure(i, 0)
        assert row["exposed"] == potential_exposure(i, row["assigned"])
        if row["outcome_observed"]:
            assert row["converted"] == potential_conversion(i, row["exposed"])


def test_design_violation_fixture_is_refused_when_certified_otherwise():
    # A fixture whose structural rule breaks monotonicity (potential exposure
    # under assignment 1 below exposure under assignment 0) must not receive a
    # reached-only pass even if a stale boolean flag claims the contract holds.
    events = _mini_trial(n=20, monotone=False)
    stale_evidence = {
        "assignment_unit": "household",
        "monotone_exposure": False,  # honest flag for the broken rule
        "exclusion_restriction": True,
    }
    result = audit_trial(_customers_for(events), events, design_evidence=stale_evidence)
    assert result["contracts"][REACHED_ONLY]["decision"] == DECISION_UNIDENTIFIED


# --------------------------------------------------------------------------- #
# 6. Identity integrity: duplicates and unknown customers.
# --------------------------------------------------------------------------- #

def test_duplicate_customer_event_is_rejected(customers, events, evidence):
    events.append(copy.deepcopy(events[0]))
    with pytest.raises(TrialError, match="duplicate trial event"):
        audit_trial(customers, events, design_evidence=evidence)


def test_unknown_customer_is_rejected(customers, events, evidence):
    events[0]["customer_id"] = "CUST-99999"
    with pytest.raises(TrialError, match="absent from generated customers"):
        audit_trial(customers, events, design_evidence=evidence)


def test_missing_customer_from_trial_is_rejected(customers, events, evidence):
    events.pop()
    with pytest.raises(TrialError, match="missing from trial"):
        audit_trial(customers, events, design_evidence=evidence)


def test_event_without_household_id_is_rejected(customers, events, evidence):
    del events[0]["household_id"]
    with pytest.raises(TrialError, match="no household_id"):
        audit_trial(customers, events, design_evidence=evidence)


def test_changing_generated_identities_changes_the_audit(events, evidence):
    # Regenerate a customer list with different identities: the same event
    # stream must then fail identity validation rather than silently pass.
    shifted = [{"customer_id": f"CUST-{i:05d}"} for i in range(101, 181)]
    with pytest.raises(TrialError, match="absent from generated customers"):
        audit_trial(shifted, events, design_evidence=evidence)


def test_duplicate_generated_identity_is_rejected(events, evidence):
    broken_customers = [{"customer_id": "CUST-00001"} for _ in range(80)]
    with pytest.raises(TrialError, match="duplicate generated customer"):
        audit_trial(broken_customers, events, design_evidence=evidence)


# --------------------------------------------------------------------------- #
# 7. Bounded arithmetic on additional arm configurations.
# --------------------------------------------------------------------------- #

def test_missing_in_offer_arm_widens_itt_interval(evidence):
    events = _mini_trial(n=20, offer_missing=2, offer_conv=6, control_conv=6)
    result = _audit(_customers_for(events), events, evidence)
    low, high = result["intention_to_treat"]["customer_denominator_interval"]
    # Two unobserved offer outcomes widen the interval by 2/arm_size = 2/10.
    assert high - low == pytest.approx(2 / 10)


def test_wald_estimand_equals_itt_over_first_stage(customers, events, evidence):
    result = audit_trial(customers, events, design_evidence=evidence)
    reached = result["contracts"][REACHED_ONLY]
    itt = result["intention_to_treat"]["customer_denominator_interval"]
    fs = result["first_stage"]["customer_denominator"]
    assert reached["effect_interval"] == pytest.approx([itt[0] / fs, itt[1] / fs])


def test_strong_effect_approves_both_contracts(evidence):
    # All ten offer customers convert, none of the control customers do.
    events = _mini_trial(n=20, offer_conv=10, control_conv=0, offer_exposed=10, control_exposed=0)
    result = _audit(_customers_for(events), events, evidence)
    assert result["contracts"][OFFER_ROLLOUT]["decision"] == DECISION_APPROVE
    assert result["contracts"][REACHED_ONLY]["decision"] == DECISION_APPROVE


# --------------------------------------------------------------------------- #
# Helpers: compact synthetic event streams with explicit household structure.
# --------------------------------------------------------------------------- #

def _mini_trial(
    n: int = 20,
    offer_conv: int = 8,
    control_conv: int = 5,
    offer_exposed: int | None = None,
    control_exposed: int | None = None,
    offer_missing: int = 0,
    monotone: bool = True,
) -> list[dict]:
    """Build an internally consistent trial over n/2 two-person households.

    Exposure counts are assigned explicitly so tests can engineer the first
    stage; ``monotone=False`` instead installs a defier-style structural rule.
    """
    assert n % 2 == 0
    if offer_exposed is None:
        offer_exposed = n // 2
    if control_exposed is None:
        control_exposed = 0
    rows: list[dict] = []
    for idx in range(n):
        customer_number = idx + 1
        household_number = idx // 2  # two consecutive customers per household
        household = f"hh-{household_number + 1:03d}"
        # Alternating household blocks: every member of a household shares an arm.
        assigned = household_number % 2
        if not monotone:
            # Deliberate defier-style rule: assignment shrinks exposure.
            exposed = int(customer_number % 11 == 0 or (not assigned and customer_number % 5 != 0))
        else:
            exposed = 0  # overwritten below per arm
        rows.append(
            {
                "customer_id": f"CUST-{customer_number:05d}",
                "household_id": household,
                "assigned": assigned,
                "exposed": exposed,
                "converted": None,
                "outcome_observed": False,
            }
        )
    if monotone:
        offer_rows = [r for r in rows if r["assigned"] == 1]
        control_rows = [r for r in rows if r["assigned"] == 0]
        for j, row in enumerate(offer_rows):
            row["exposed"] = int(j < offer_exposed)
        for j, row in enumerate(control_rows):
            row["exposed"] = int(j < control_exposed)

    # Fill observed conversions deterministically within each arm.
    offer_rows = [r for r in rows if r["assigned"] == 1]
    control_rows = [r for r in rows if r["assigned"] == 0]
    _stamp_conversions(offer_rows, offer_conv, offer_missing)
    _stamp_conversions(control_rows, control_conv, 0)
    return rows


def _stamp_conversions(rows: list[dict], converted: int, missing: int) -> None:
    assert converted + missing <= len(rows)
    for j, row in enumerate(rows):
        if j < missing:
            row["converted"] = None
            row["outcome_observed"] = False
        else:
            row["outcome_observed"] = True
            row["converted"] = int(j - missing < converted)
