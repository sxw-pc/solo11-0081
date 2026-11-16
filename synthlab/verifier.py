"""Independent verification of an exported dataset directory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constraints import check_dataset
from .errors import SynthlabError
from .generators.common import as_text
from .manifest import compute_fingerprint, read_manifest, table_data_sha256, table_digest, utc_now_text
from .readers import OutputReadError, read_output_tables, safe_output_path
from .report import write_verification_reports
from .spec.loader import load_dataset
from .spec.model import DatasetSpec, TableData


CONSTRAINT_CODES = {
    "CST001": "VER004",
    "CST002": "VER005",
    "CST003": "VER006",
    "CST004": "VER006",
    "CST005": "VER006",
    "CST006": "VER006",
    "CST007": "VER007",
    "CST008": "VER006",
    "CST009": "VER009",
    "CST010": "VER013",
}


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    id: str
    status: str
    message: str
    code: str | None = None
    path: str | None = None
    table: str | None = None
    column: str | None = None

    @property
    def location(self) -> str | None:
        parts = [part for part in (self.path, self.table, self.column) if part]
        return ":".join(parts) if parts else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "code": self.code or "OK",
            "message": self.message,
            "path": self.path,
            "table": self.table,
            "column": self.column,
            "location": self.location,
        }


@dataclass(slots=True)
class VerificationResult:
    out_dir: Path
    dataset: str | None
    checks: list[VerificationCheck] = field(default_factory=list)
    boundary: dict[str, Any] = field(default_factory=dict)
    content_fingerprint: str | None = None
    checked_at: str = field(default_factory=utc_now_text)

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def add(self, id: str, status: str, message: str, code: str | None = None,
            path: str | None = None, table: str | None = None, column: str | None = None) -> None:
        self.checks.append(VerificationCheck(id, status, message, code, path, table, column))

    def as_dict(self) -> dict[str, Any]:
        failed = [check.as_dict() for check in self.checks if check.status == "fail"]
        passed = sum(1 for check in self.checks if check.status == "pass")
        payload = {
            "status": "pass" if self.ok else "fail",
            "dataset": self.dataset,
            "checked_at": self.checked_at,
            "content_fingerprint": self.content_fingerprint,
            "summary": {"total": len(self.checks), "passed": passed, "failed": len(self.checks) - passed},
            "checks": [check.as_dict() for check in self.checks],
            "boundary_coverage": self.boundary,
            "issues": failed,
        }
        return payload

    def write(self) -> tuple[Path, Path]:
        return write_verification_reports(self.out_dir, self.as_dict())


def _manifest_shape(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return "manifest root must be an object"
    required = ("dataset", "blueprint", "outputs", "tables", "content_fingerprint")
    for key in required:
        if key not in manifest:
            return f"manifest is missing {key!r}"
    if not isinstance(manifest["dataset"], dict):
        return "manifest dataset must be an object"
    if not isinstance(manifest["blueprint"], dict):
        return "manifest blueprint must be an object"
    if not isinstance(manifest["outputs"], list):
        return "manifest outputs must be a list"
    if not isinstance(manifest["tables"], list):
        return "manifest tables must be a list"
    if not isinstance(manifest["content_fingerprint"], str):
        return "manifest content_fingerprint must be a string"
    return None


def _verify_blueprint(
    result: VerificationResult, manifest: dict[str, Any], out_dir: Path, blueprint_path: str | None
) -> DatasetSpec | None:
    blueprint_record = manifest["blueprint"]
    relative = blueprint_record.get("file")
    if blueprint_path:
        path = Path(blueprint_path)
    elif isinstance(relative, str):
        try:
            path = safe_output_path(out_dir, relative)
        except OutputReadError as exc:
            result.add("blueprint", "fail", str(exc), "VER012", path=relative)
            return None
    else:
        result.add("blueprint", "fail", "manifest does not identify a blueprint copy", "VER012")
        return None
    try:
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        result.add("blueprint", "fail", f"cannot read blueprint: {exc}", "VER012", path=str(path))
        return None
    expected_sha = blueprint_record.get("sha256")
    if actual_sha != expected_sha:
        result.add("blueprint", "fail", "blueprint sha256 does not match manifest", "VER012", path=str(path))
        return None
    try:
        dataset = load_dataset(path)
    except SynthlabError as exc:
        result.add("blueprint", "fail", str(exc), exc.code, path=str(path))
        return None
    result.add("blueprint", "pass", f"blueprint loaded, sha256 {actual_sha[:12]}...", path=str(path))
    if dataset.blueprint_sha256 != expected_sha:
        result.add("manifest", "fail", "dataset blueprint hash disagrees with manifest", "VER010")
    metadata = manifest["dataset"]
    if (metadata.get("name"), metadata.get("version"), metadata.get("seed")) != (
        dataset.name, dataset.version, dataset.seed
    ):
        result.add("manifest", "fail", "dataset name/version/seed disagree with blueprint", "VER010")
    return dataset


def _verify_outputs(
    result: VerificationResult, manifest: dict[str, Any], out_dir: Path
) -> tuple[list[dict[str, Any]], bool]:
    actual: list[dict[str, Any]] = []
    complete = True
    for record in manifest["outputs"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            result.add("output", "fail", "malformed output record", "VER010")
            complete = False
            continue
        relative = record["path"]
        check_id = f"file:{relative}"
        try:
            path = safe_output_path(out_dir, relative)
        except OutputReadError as exc:
            result.add(check_id, "fail", str(exc), "VER001", path=relative)
            complete = False
            continue
        if not path.is_file():
            result.add(check_id, "fail", "output file is missing", "VER001", path=relative)
            complete = False
            continue
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        actual.append({"path": relative, "bytes": len(payload), "sha256": digest})
        if len(payload) != record.get("bytes") or digest != record.get("sha256"):
            result.add(check_id, "fail", "file bytes or sha256 does not match manifest", "VER002", path=relative)
            complete = False
        else:
            result.add(check_id, "pass", "file hash matches manifest", path=relative)
    return actual, complete


def _verify_tables(
    result: VerificationResult,
    dataset: DatasetSpec,
    manifest: dict[str, Any],
    out_dir: Path,
) -> tuple[dict[str, TableData], bool]:
    tables, errors = read_output_tables(out_dir, manifest, dataset)
    manifest_tables = {
        entry.get("name"): entry for entry in manifest["tables"] if isinstance(entry, dict)
    }
    expected_names = {table.name for table in dataset.tables}
    if set(manifest_tables) != expected_names:
        result.add("manifest:tables", "fail", "manifest table list disagrees with blueprint", "VER010")
    complete = True
    for table in dataset.tables:
        if table.name in errors:
            result.add(f"table:{table.name}", "fail", errors[table.name], "VER001", table=table.name)
            complete = False
            continue
        data = tables[table.name]
        entry = manifest_tables.get(table.name)
        if not isinstance(entry, dict):
            result.add(f"table:{table.name}:rows", "fail", "table is absent from manifest", "VER010",
                       table=table.name)
            complete = False
            continue
        if data.row_count != entry.get("rows"):
            result.add(f"table:{table.name}:rows", "fail",
                       f"row count {data.row_count} does not match manifest {entry.get('rows')}",
                       "VER003", table=table.name)
            complete = False
        else:
            result.add(f"table:{table.name}:rows", "pass", f"{data.row_count} rows match manifest",
                       table=table.name)
        digest = table_data_sha256(data)
        if digest != entry.get("data_sha256"):
            result.add(f"table:{table.name}:data", "fail", "table data sha256 does not match manifest",
                       "VER011", table=table.name)
            complete = False
        else:
            result.add(f"table:{table.name}:data", "pass", "table data sha256 matches manifest",
                       table=table.name)
    return tables, complete


def _add_constraint_checks(result: VerificationResult, dataset: DatasetSpec,
                           tables: dict[str, TableData]) -> None:
    report = check_dataset(dataset, tables)
    result.boundary = {
        table: {column: item.as_dict() for column, item in columns.items()}
        for table, columns in report.boundary.items()
    }
    for index, issue in enumerate(report.issues):
        if issue.code == "CST009":
            continue
        result.add(
            f"constraint:{index + 1}",
            "fail",
            issue.message,
            CONSTRAINT_CODES.get(issue.code, "VER006"),
            table=issue.table,
            column=issue.column,
        )
    for table, columns in report.boundary.items():
        for column, item in columns.items():
            failed = bool(dataset.require_boundary_coverage and item.missing)
            message = f"coverage {item.coverage:.0%}"
            if item.missing:
                message += f"; missing {list(item.missing)}"
            if not dataset.require_boundary_coverage and item.missing:
                message += " (report only)"
            result.add(f"boundary:{table}.{column}", "fail" if failed else "pass",
                       message, "VER009" if failed else None, table=table, column=column)


def _expected_distributions(dataset: DatasetSpec) -> list[dict[str, Any]]:
    """Declaration-level distribution entries derived from the blueprint."""
    expected: list[dict[str, Any]] = []
    for table in dataset.tables:
        for column in table.columns:
            dist = column.distribute
            if dist is None or column.references is None:
                continue
            expected.append({
                "table": table.name,
                "column": column.name,
                "parent": column.references.table,
                "parent_column": column.references.column,
                "by": dist.attribute,
                "mode": dist.mode,
                "route": dist.route,
                "unlisted": dist.unlisted,
                "declared": {as_text(value): amount for value, amount in dist.entries},
            })
    return sorted(expected, key=lambda entry: (entry["table"], entry["column"]))


_DECLARATION_KEYS = ("table", "column", "parent", "parent_column", "by", "mode", "route",
                     "unlisted", "declared")


def _verify_distributions(result: VerificationResult, dataset: DatasetSpec,
                          manifest: dict[str, Any]) -> None:
    """Cross-check the manifest's distribution section against the blueprint.

    The manifest records which assignment route was selected and which
    business distribution must hold; the row-level recomputation itself
    happens in the constraint checks (CST010 -> VER013).
    """
    expected = _expected_distributions(dataset)
    recorded = manifest.get("distributions") or []
    recorded_declarations = [
        {key: entry.get(key) for key in _DECLARATION_KEYS}
        for entry in recorded
        if isinstance(entry, dict)
    ]
    recorded_sorted = sorted(
        recorded_declarations, key=lambda entry: (str(entry.get("table")), str(entry.get("column")))
    )
    if not expected and not recorded:
        return
    if recorded_sorted != expected:
        result.add("distributions", "fail",
                   "manifest distributions do not match blueprint declarations", "VER010")
        return
    result.add("distributions", "pass",
               f"{len(expected)} distribution declaration(s) recorded in manifest")


def verify_dataset(out_dir: str | Path, blueprint_path: str | None = None) -> VerificationResult:
    target = Path(out_dir)
    result = VerificationResult(target, None)
    manifest_path = target / "manifest.json"
    try:
        manifest = read_manifest(manifest_path)
    except SynthlabError as exc:
        code = "VER001" if not manifest_path.is_file() else "VER010"
        result.add("manifest", "fail", str(exc), code, path=str(manifest_path))
        return result
    shape_error = _manifest_shape(manifest)
    if shape_error:
        result.add("manifest", "fail", shape_error, "VER010")
        return result
    result.add("manifest", "pass", "manifest.json parsed")
    result.dataset = manifest["dataset"].get("name")
    result.content_fingerprint = manifest["content_fingerprint"]
    dataset = _verify_blueprint(result, manifest, target, blueprint_path)
    actual_outputs, outputs_complete = _verify_outputs(result, manifest, target)
    if dataset is None:
        return result
    tables, tables_complete = _verify_tables(result, dataset, manifest, target)
    _add_constraint_checks(result, dataset, tables)
    _verify_distributions(result, dataset, manifest)
    if tables_complete and outputs_complete and len(tables) == len(dataset.tables):
        digests = [table_digest(data) for data in tables.values()]
        actual_fingerprint = compute_fingerprint(dataset, digests, actual_outputs)
        if actual_fingerprint == manifest["content_fingerprint"]:
            result.add("fingerprint", "pass", "content fingerprint recomputed successfully")
        else:
            result.add("fingerprint", "fail", "content fingerprint does not match manifest", "VER008")
    else:
        result.add("fingerprint", "fail",
                   "content fingerprint cannot be recomputed from incomplete outputs", "VER008")
    return result
