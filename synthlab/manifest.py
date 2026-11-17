"""Manifest construction and content fingerprinting."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .exporters import OutputFile
from .spec.model import DatasetData, TableData

MANIFEST_VERSION = 1


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_data_sha256(table: TableData) -> str:
    payload = {
        "name": table.name,
        "columns": list(table.columns),
        "rows": [list(row) for row in table.rows],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def table_digest(table: TableData) -> dict[str, Any]:
    return {
        "name": table.name,
        "row_count": table.row_count,
        "columns": list(table.columns),
        "data_sha256": table_data_sha256(table),
    }


def compute_fingerprint(
    dataset: Any,
    table_digests: Iterable[dict[str, Any]],
    outputs: Iterable[dict[str, Any]],
) -> str:
    normalized_tables = sorted(
        (
            {
                "name": entry["name"],
                "row_count": entry["row_count"],
                "columns": list(entry["columns"]),
                "data_sha256": entry["data_sha256"],
            }
            for entry in table_digests
        ),
        key=lambda entry: entry["name"],
    )
    normalized_outputs = sorted(
        (
            {
                "path": entry["path"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
            }
            for entry in outputs
        ),
        key=lambda entry: entry["path"],
    )
    payload = {
        "blueprint_sha256": dataset.blueprint_sha256,
        "dataset": dataset.name,
        "seed": dataset.seed,
        "version": dataset.version,
        "tables": normalized_tables,
        "outputs": normalized_outputs,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def build_manifest(
    data: DatasetData,
    outputs: Iterable[OutputFile],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    output_records = [output.as_dict() for output in outputs]
    digests = [table_digest(data.tables[name]) for name in data.table_order]
    blueprint_output = next((item for item in output_records if item["format"] == "blueprint"), None)
    tables: list[dict[str, Any]] = []
    for name in data.table_order:
        table_data = data.tables[name]
        table_spec = data.dataset.table(name)
        tables.append({
            "name": name,
            "rows": table_data.row_count,
            "primary_key": table_spec.primary_key if table_spec else None,
            "column_stats": data.column_stats.get(name, {}),
            "data_sha256": next(item["data_sha256"] for item in digests if item["name"] == name),
        })
    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset": {
            "name": data.dataset.name,
            "version": data.dataset.version,
            "seed": data.dataset.seed,
            "require_boundary_coverage": data.dataset.require_boundary_coverage,
        },
        "blueprint": {
            "file": blueprint_output["path"] if blueprint_output else None,
            "sha256": data.dataset.blueprint_sha256,
        },
        "created_at": created_at or utc_now_text(),
        "formats": sorted({item["format"] for item in output_records if item["format"] != "blueprint"}),
        "tables": tables,
        "boundary_coverage": {
            table: {column: result.as_dict() for column, result in columns.items()}
            for table, columns in data.boundary.items()
        },
        "outputs": sorted(output_records, key=lambda item: item["path"]),
        "content_fingerprint": compute_fingerprint(data.dataset, digests, output_records),
    }


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return destination


def read_manifest(path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from .errors import SynthlabError

        raise SynthlabError("VER010", f"cannot read manifest: {exc}", path=str(path)) from exc
