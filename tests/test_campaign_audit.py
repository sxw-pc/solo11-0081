import json
from pathlib import Path
from synthlab.campaign_audit import audit_trial

def test_campaign_fixture_ids():
    events = json.loads((Path(__file__).parents[1]/"fixtures/campaign/trial.json").read_text())["events"]
    customers = [{"customer_id": f"CUST-{i:05d}"} for i in range(1,81)]
    result = audit_trial(customers, events)
    assert result["unobserved_customers"] == 0
    assert sum(v[2] for v in result["counts"].values()) == 80
    assert sum(v[1] for v in result["counts"].values()) == 79
