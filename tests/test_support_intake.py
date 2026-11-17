import json
from pathlib import Path
from synthlab.support_intake import assign_cases

def test_support_intake_fixture():
    data=json.loads((Path(__file__).parents[1]/"fixtures/support/intake.json").read_text())
    assert [r["agent_id"] for r in assign_cases(data["cases"],data["agents"])]==["a-1","a-1","a-1"]
