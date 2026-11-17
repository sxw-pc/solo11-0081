from synthlab.return_intake import assess_returns
import json
from pathlib import Path

def test_return_intake_fixture():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/returns/intake.json").read_text())
    decisions = assess_returns(fixture["orders"], fixture["items"], fixture["claims"])
    assert [d["status"] for d in decisions] == [
        "accepted", "accepted", "rejected", "accepted", "rejected"]
    assert [d["refund_cap"] for d in decisions] == [
        "25.00", "25.00", None, "40.00", None]
