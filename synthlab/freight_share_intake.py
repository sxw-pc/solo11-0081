"""Freight intake entry point: solo quotes, exact shared routing and invoices."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import SynthlabError
from .freight_sharing import (
    ContractRejectedError,
    FreightInfeasibleError,
    FreightValidationError,
    settle,
    solo_quotes,
)

__all__ = ["solo_quotes", "main", "build_report"]


def build_report(fixture_path: Path, dataset_path: Path | None = None) -> dict:
    """Load a fixture (and optionally a catalog blueprint) and settle the batch."""
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    valid_skus = None
    if dataset_path is not None:
        # Imported lazily so pure freight runs need no YAML/dataset dependency.
        from .engine import generate_dataset
        from .spec.loader import load_dataset

        generated = generate_dataset(load_dataset(dataset_path))
        valid_skus = {
            row[0] for row in generated.tables["products"].rows
        }
    return settle(data, valid_skus=valid_skus)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Minimum-cost shared freight routing and contract invoices."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("fixtures/freight_share/intake.json"),
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="optional dataset blueprint used to validate shipment SKU identities",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args.fixture, args.dataset)
    except FreightValidationError as exc:
        print(f"rejected intake: {exc}", file=sys.stderr)
        return 2
    except FreightInfeasibleError as exc:
        print(f"infeasible routing: {exc}", file=sys.stderr)
        return 4
    except ContractRejectedError as exc:
        print(f"contract rejected: {exc}", file=sys.stderr)
        return 3
    except SynthlabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, sort_keys=True, indent=2))
    rejected = [
        name
        for name, contract in report["contracts"].items()
        if contract["status"] != "issued"
    ]
    return 3 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
