import json
from pathlib import Path
from synthlab.support_intake import assign_cases
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset


def test_surge_cases_use_generated_orders():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/support/surge.json").read_text())
    orders=generate_dataset(load_dataset(root/"fixtures/demo/dataset.yaml")).tables["orders"].rows
    valid={r[0] for r in orders}
    cases=data["cases"]+[s["case"] for s in data["surge_scenarios"]]
    assert all(c["order_id"] in valid for c in cases)


def test_first_eligible_preview_overbooks_agent():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/support/surge.json").read_text())
    for scenario in data["surge_scenarios"]:
        cases=data["cases"]+[scenario["case"]]
        result=assign_cases(cases,data["agents"])
        assert [r["agent_id"] for r in result]==["a-1"]*4
        assert sum(c["resolve_minutes"] for c in cases)==9
        assert sum(a["shift_end"]-a["shift_start"] for a in data["agents"])==5
