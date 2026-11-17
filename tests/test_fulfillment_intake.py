from synthlab.fulfillment_intake import plan_fulfillment
import json
from pathlib import Path

def test_fulfillment_intake():
    fixture = json.loads((Path(__file__).parents[1]/"fixtures/fulfillment/intake.json").read_text())
    result = plan_fulfillment(fixture["orders"], fixture["items"], fixture["stock"])
    assert result[0]["status"] == "allocated"
    assert result[1]["status"] == "backorder"
    assert result[2]["status"] == "not_paid"
