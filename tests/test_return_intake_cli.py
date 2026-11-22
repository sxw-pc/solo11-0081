"""CLI tests for legacy intake and the settlement entry point."""
from __future__ import annotations

import json
from pathlib import Path

from synthlab.return_intake import main

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_cli_keeps_cap_output(capsys):
    rc = main(["--fixture", str(ROOT / "fixtures" / "returns" / "intake.json")])
    assert rc == 0
    decisions = json.loads(capsys.readouterr().out)
    assert [d["refund_cap"] for d in decisions] == [
        "25.00", "25.00", None, "40.00", None]


def test_settlement_cli_emits_audit_decisions_and_ledger(capsys, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    rc = main([
        "--fixture", str(ROOT / "fixtures" / "returns" / "settlement.json"),
        "--ledger-out", str(ledger_path),
    ])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["audit"]["status"] == "coherent"
    assert {d["request_id"] for d in report["decisions"]}
    # Canonical retain_promotion / reprice_kept payouts appear in the report.
    by_id = {d["request_id"]: d for d in report["decisions"]}
    assert by_id["q-100-1"]["cash_refund"] == "24.93"
    assert by_id["q-400-1"]["cash_refund"] == "21.85"
    assert by_id["q-200-4"]["reason"] == "quantity_exceeded"
    ledger = json.loads(ledger_path.read_text())
    assert "o-100" in ledger["orders"]
    assert ledger["orders"]["o-100"]["promotion_contract"] == "retain_promotion"
    # Rejected requests are recorded too, but carry no money.
    assert ledger["requests"]["q-200-4"]["amount"] == "0.00"


def test_settlement_cli_aborts_on_broken_snapshot(tmp_path):
    fixture = json.loads(
        (ROOT / "fixtures" / "returns" / "settlement.json").read_text())
    fixture["payments"][0]["captured_amount"] = "1.00"
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(fixture), encoding="utf-8")
    from synthlab.settlement import SettlementError
    import pytest
    with pytest.raises(SettlementError, match="original-sale audit"):
        main(["--fixture", str(broken)])
