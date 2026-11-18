"""CLI behavior for the campaign trial adjudication entry point."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthlab import campaign_audit

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / "fixtures" / "campaign" / "trial.json"
SPEC = ROOT / "fixtures" / "demo" / "dataset.yaml"


def test_cli_text_report_contains_target_decisions(capsys):
    rc = campaign_audit.main(["--trial", str(TRIAL), "--spec", str(SPEC)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "household = randomization unit" in out
    assert "ITT" in out
    assert "first stage" in out
    assert "naive" in out
    assert "offer_rollout" in out and "reached_only" in out
    assert "decision=reject" in out
    assert "decision=approve" in out
    assert "synthetic sample only" in out


def test_cli_json_report_matches_manifest_targets(capsys):
    rc = campaign_audit.main(["--trial", str(TRIAL), "--spec", str(SPEC), "--json"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["intention_to_treat"]["customer_denominator_interval"] == pytest.approx(
        [0.275, 0.3]
    )
    assert result["first_stage"]["customer_denominator"] == pytest.approx(0.725)
    assert result["contracts"]["offer_rollout"]["decision"] == "reject"
    assert result["contracts"]["reached_only"]["decision"] == "approve"
    assert result["contracts"]["reached_only"]["effect_interval"] == pytest.approx(
        [0.3793103448275862, 0.41379310344827586]
    )


def test_cli_rejects_unknown_customer_fixture(tmp_path, capsys):
    payload = json.loads(TRIAL.read_text(encoding="utf-8"))
    payload["events"][0]["customer_id"] = "CUST-99999"
    broken = tmp_path / "broken_trial.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(campaign_audit.TrialError, match="absent from generated customers"):
        campaign_audit.main(["--trial", str(broken), "--spec", str(SPEC)])
