"""Atomic ad-slot preview for a synthetic product."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


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
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/ad_auction/intake.json"))
    args=p.parse_args(argv)
    print(json.dumps(atomic_preview(json.loads(args.fixture.read_text()))))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
