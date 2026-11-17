"""First-eligible-agent intake for synthetic commerce support cases."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

def assign_cases(cases,agents):
    assignments=[]
    for case in cases:
        eligible=[a for a in agents if case["skill"] in a["skills"]
                  and case["language"] in a["languages"]]
        eligible.sort(key=lambda a:a["agent_id"])
        assignments.append({"case_id":case["case_id"],
                            "agent_id":eligible[0]["agent_id"] if eligible else None,
                            "status":"assigned" if eligible else "unassigned"})
    return assignments

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixture",type=Path,default=Path("fixtures/support/intake.json"))
    args=parser.parse_args(argv)
    data=json.loads(args.fixture.read_text(encoding="utf-8"))
    print(json.dumps(assign_cases(data["cases"],data["agents"]),sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
