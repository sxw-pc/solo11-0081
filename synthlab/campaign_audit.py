"""Synthetic campaign trial audit over generated customer identities."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .engine import generate_dataset
from .spec.loader import load_dataset

def audit_trial(customers, events):
    ids = {row["customer_id"] for row in customers}
    if len(ids) != len(customers):
        raise ValueError("duplicate generated customer")
    seen = set()
    groups = {"exposed": [0, 0, 0], "unexposed": [0, 0, 0]}
    for event in events:
        cid = event["customer_id"]
        if cid not in ids or cid in seen:
            raise ValueError("missing or duplicate trial customer")
        seen.add(cid)
        key = "exposed" if event["exposed"] else "unexposed"
        groups[key][2] += 1
        if event["outcome_observed"]:
            groups[key][1] += 1
            groups[key][0] += int(event["converted"])
    return {"counts": groups, "unobserved_customers": len(ids - seen)}

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=Path("fixtures/demo/dataset.yaml"))
    parser.add_argument("--trial", type=Path, default=Path("fixtures/campaign/trial.json"))
    args = parser.parse_args(argv)
    data = generate_dataset(load_dataset(args.spec))
    table = data.tables["customers"]
    customers = [dict(zip(table.columns, row)) for row in table.rows]
    events = json.loads(args.trial.read_text(encoding="utf-8"))["events"]
    print(json.dumps(audit_trial(customers, events), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
