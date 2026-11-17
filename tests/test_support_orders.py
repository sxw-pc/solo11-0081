import json
from pathlib import Path
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset

def test_support_cases_reference_generated_orders():
    root=Path(__file__).parents[1]
    data=generate_dataset(load_dataset(root/"fixtures/demo/dataset.yaml"))
    ids={row[0] for row in data.tables["orders"].rows}
    cases=json.loads((root/"fixtures/support/intake.json").read_text())["cases"]
    assert {c["order_id"] for c in cases} <= ids
