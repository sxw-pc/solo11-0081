import json
from pathlib import Path
from synthlab.credit_intake import screen_individually
from synthlab.engine import generate_dataset
from synthlab.spec.loader import load_dataset


def test_invoice_customers_are_generated_demo_customers():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/credit/intake.json").read_text())
    customers=generate_dataset(load_dataset(root/"fixtures/demo/dataset.yaml")).tables["customers"].rows
    ids={row[0] for row in customers}
    assert {row["customer_id"] for row in data["invoices"]}<=ids


def test_local_screening_misses_joint_default_scenario():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/credit/intake.json").read_text())
    assert screen_individually(data,"cash_deposit")==["inv-A","inv-B","inv-C"]
    assert screen_individually(data,"shared_guarantee")==["inv-A","inv-B","inv-C"]
    by_id={row["invoice_id"]:row for row in data["invoices"]}
    ab=next(s for s in data["scenarios"] if s["scenario_id"]=="shock-AB")
    deposit_loss=sum(by_id[i]["principal"]-by_id[i]["deposit"] for i in ab["defaults"])
    guarantee_loss=max(0,sum(by_id[i]["principal"] for i in ab["defaults"])-50)
    assert deposit_loss==80>40
    assert guarantee_loss==50>40
