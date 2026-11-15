"""Command line interface for synthlab."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .constraints import ConstraintReport, check_dataset
from .engine import generate_dataset
from .errors import SynthlabError
from .exporters import FORMATS, OutputFile, export_dataset
from .manifest import build_manifest, write_manifest
from .planner import plan_dataset
from .preview import serve_preview
from .spec.loader import load_dataset
from .spec.model import DatasetData, DatasetSpec
from .verifier import VerificationResult, verify_dataset

DEFAULT_FORMATS = ",".join(FORMATS)


def _project_root() -> Path:
    package_root = Path(__file__).resolve().parent.parent
    if (package_root / "fixtures" / "demo" / "dataset.yaml").is_file():
        return package_root
    return Path.cwd()


def demo_blueprint() -> Path:
    return _project_root() / "fixtures" / "demo" / "dataset.yaml"


def _formats(value: str) -> str:
    return value


def _run_generation(
    spec: DatasetSpec,
    out_dir: str | Path,
    formats: str = DEFAULT_FORMATS,
) -> tuple[DatasetData, ConstraintReport, dict[str, Any], tuple[OutputFile, ...]]:
    plan = plan_dataset(spec)
    data = generate_dataset(spec, plan)
    report = check_dataset(spec, data.tables)
    if report.issues:
        raise SynthlabError(
            report.issues[0].code,
            "generated data failed constraints; nothing was exported",
            issues=report.issues,
        )
    outputs = export_dataset(data, out_dir, formats, blueprint_bytes=spec.blueprint_bytes)
    manifest = build_manifest(data, outputs)
    write_manifest(Path(out_dir) / "manifest.json", manifest)
    return data, report, manifest, outputs


def _print_generation(
    data: DatasetData, report: ConstraintReport, manifest: dict[str, Any], out_dir: str | Path
) -> None:
    print(f"dataset: {data.dataset.name} version {data.dataset.version} seed {data.dataset.seed}")
    print("tables:")
    for name in data.table_order:
        boundary_count = sum(len(columns) for columns in [report.boundary.get(name, {})])
        print(f"  {name}: {data.tables[name].row_count} rows, {boundary_count} boundary column(s)")
    print(f"output: {Path(out_dir).resolve()}")
    print(f"formats: {', '.join(manifest['formats'])}")
    print(f"content_fingerprint: {manifest['content_fingerprint']}")
    missing = [
        f"{table}.{column}:{list(item.missing)}"
        for table, columns in report.boundary.items()
        for column, item in columns.items()
        if item.missing
    ]
    if missing:
        print("boundary notes (report only unless required): " + "; ".join(missing))


def _print_verification(result: VerificationResult) -> None:
    payload = result.as_dict()
    summary = payload["summary"]
    print(f"verification: {payload['status']} ({summary['passed']} passed, {summary['failed']} failed)")
    for check in payload["checks"]:
        if check["status"] == "fail":
            location = f" [{check['location']}]" if check.get("location") else ""
            print(f"  FAIL {check['code']} {check['id']}{location}: {check['message']}")
    print("reports: verification.json, verification.md")


def _command_demo(args: argparse.Namespace) -> int:
    spec = load_dataset(demo_blueprint())
    data, report, manifest, _ = _run_generation(spec, args.out, args.formats)
    _print_generation(data, report, manifest, args.out)
    verification = verify_dataset(args.out)
    verification.write()
    _print_verification(verification)
    code = 0 if verification.ok else 1
    print(f"exit code: {code}")
    return code


def _command_generate(args: argparse.Namespace) -> int:
    spec = load_dataset(args.spec)
    data, report, manifest, _ = _run_generation(spec, args.out, args.formats)
    _print_generation(data, report, manifest, args.out)
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    result = verify_dataset(args.out, args.spec)
    result.write()
    _print_verification(result)
    return 0 if result.ok else 1


def _command_plan(args: argparse.Namespace) -> int:
    spec = load_dataset(args.spec)
    print(plan_dataset(spec).readable())
    return 0


def _command_serve(args: argparse.Namespace) -> int:
    serve_preview(args.out, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthlab",
        description="Offline, deterministic multi-table synthetic data workbench.",
    )
    parser.add_argument("--version", action="version", version=f"synthlab {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="generate the fixture dataset and verify it")
    demo.add_argument("--out", default="out/demo", help="output directory (default: out/demo)")
    demo.add_argument("--formats", default=DEFAULT_FORMATS, type=_formats,
                      help=f"comma-separated formats (default: {DEFAULT_FORMATS})")
    demo.set_defaults(func=_command_demo)

    generate = subparsers.add_parser("generate", help="generate from a YAML blueprint")
    generate.add_argument("--spec", required=True, help="path to dataset YAML")
    generate.add_argument("--out", required=True, help="output directory")
    generate.add_argument("--formats", default=DEFAULT_FORMATS, type=_formats,
                          help=f"comma-separated formats (default: {DEFAULT_FORMATS})")
    generate.set_defaults(func=_command_generate)

    verify = subparsers.add_parser("verify", help="independently verify an output directory")
    verify.add_argument("--out", required=True, help="generated output directory")
    verify.add_argument("--spec", default=None, help="optional blueprint override")
    verify.set_defaults(func=_command_verify)

    plan = subparsers.add_parser("plan", help="print the deterministic execution plan")
    plan.add_argument("--spec", required=True, help="path to dataset YAML")
    plan.set_defaults(func=_command_plan)

    serve = subparsers.add_parser("serve", help="serve a read-only preview on 127.0.0.1")
    serve.add_argument("--out", default="out/demo", help="generated output directory")
    serve.add_argument("--port", type=int, default=8000, help="loopback port (default: 8000)")
    serve.set_defaults(func=_command_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SynthlabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
