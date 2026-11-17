"""Synthetic deferred-payment intake with per-applicant screening only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def screen_individually(data: dict, contract_name: str) -> list[str]:
    """A deliberately local screening baseline, not a portfolio approval."""
    contract=data["contracts"][contract_name]
    approved=[]
    for invoice in data["invoices"]:
        losses=[]
        for scenario in data["scenarios"]:
            gross=invoice["principal"] if invoice["invoice_id"] in scenario["defaults"] else 0
            if contract_name=="cash_deposit":
                losses.append(max(0,gross-invoice["deposit"]))
            else:
                losses.append(max(0,gross-contract["guarantee_pool"]))
        if max(losses)<=contract["worst_case_limit"]:
            approved.append(invoice["invoice_id"])
    return approved


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/credit/intake.json"))
    p.add_argument("--contract",choices=("cash_deposit","shared_guarantee"),default="cash_deposit")
    args=p.parse_args(argv)
    print(json.dumps(screen_individually(json.loads(args.fixture.read_text()),args.contract)))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
