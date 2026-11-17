"""Individual round-trip quote preview for synthetic merchants."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def solo_quotes(data: dict) -> dict[str, int]:
    depot=data["depot"]
    return {m["merchant_id"]:data["distance"][depot][m["stop"]]+data["distance"][m["stop"]][depot]
            for m in data["merchants"]}


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/freight_share/intake.json"))
    args=p.parse_args(argv)
    print(json.dumps(solo_quotes(json.loads(args.fixture.read_text())),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
