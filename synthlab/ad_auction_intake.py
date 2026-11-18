"""Ad-slot auction intake for a synthetic product.

``atomic_preview`` is the legacy greedy single-slot preview; it is only
material for comparison, not an issuable ad sales contract. ``main`` runs the
full combinatorial round from :mod:`synthlab.ad_auction` and prints the
reviewable allocation and settlement certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .ad_auction import build_certificate, catalog_from_spec
from .errors import SynthlabError
from .spec.loader import load_dataset


def atomic_preview(data: dict) -> list[str]:
    """Greedily award single-slot bids; bundle bids are not considered."""
    free=set(data["slots"])
    winners=[]
    for bid in sorted(data["bids"],key=lambda b:(-b["value"],b["bid_id"])):
        if len(bid["slots"])!=1: continue
        slot=bid["slots"][0]
        if slot in free:
            free.remove(slot)
            winners.append(bid["bid_id"])
    return winners


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description="Combinatorial ad-slot auction with certificated settlement.")
    p.add_argument("--fixture",type=Path,default=Path("fixtures/ad_auction/intake.json"))
    p.add_argument("--dataset",type=Path,default=Path("fixtures/demo/dataset.yaml"),
                   help="engine blueprint that generates the product catalog")
    p.add_argument("--atomic-preview",action="store_true",
                   help="print only the legacy single-slot preview (not a sales contract)")
    args=p.parse_args(argv)
    try:
        raw=args.fixture.read_bytes()
    except OSError as exc:
        print(f"cannot read fixture: {exc}",file=sys.stderr)
        return 2
    try:
        data=json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"fixture is not valid JSON: {exc}",file=sys.stderr)
        return 2
    if args.atomic_preview:
        print(json.dumps(atomic_preview(data)))
        return 0
    try:
        spec=load_dataset(args.dataset)
        catalog=catalog_from_spec(spec)
        certificate=build_certificate(
            data,
            catalog=catalog,
            fixture={"path":str(args.fixture),"sha256":hashlib.sha256(raw).hexdigest()},
        )
    except SynthlabError as exc:
        print(json.dumps({"valid":False,"errors":[i.as_dict() for i in exc.issues]},indent=2),
              file=sys.stderr)
        return 2
    print(json.dumps(certificate,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
