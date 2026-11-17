"""Human- and tool-friendly output formats for generated data."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import ExportError
from .generators.common import as_text
from .spec.model import DatasetData, TableData
from .sqlite_export import create_sqlite

FORMATS = ("sqlite", "csv", "jsonl")
NULL_TOKEN = r"\N"


@dataclass(frozen=True, slots=True)
class OutputFile:
    path: str
    format: str
    table: str | None
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "table": self.table,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


def _digest(path: Path) -> tuple[int, str]:
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def _record(path: Path, out_dir: Path, format_name: str, table: str | None) -> OutputFile:
    size, digest = _digest(path)
    return OutputFile(path.relative_to(out_dir).as_posix(), format_name, table, size, digest)


def _text_value(value: Any) -> str:
    if value is None:
        return NULL_TOKEN
    return as_text(value)


def _write_csv(data: TableData, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(data.columns)
        for row in data.rows:
            writer.writerow([_text_value(value) for value in row])


def _write_jsonl(data: TableData, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in data.rows:
            payload = {name: value for name, value in zip(data.columns, row)}
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            handle.write("\n")


def _normalize_formats(formats: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(formats, str):
        candidates = [item.strip().lower() for item in formats.split(",") if item.strip()]
    else:
        candidates = [str(item).strip().lower() for item in formats if str(item).strip()]
    unknown = sorted(set(candidates) - set(FORMATS))
    if unknown:
        raise ExportError("EXP003", f"unknown export format(s): {', '.join(unknown)}")
    if not candidates:
        raise ExportError("EXP003", f"at least one export format is required: {', '.join(FORMATS)}")
    return tuple(dict.fromkeys(candidates))


def export_dataset(
    data: DatasetData,
    out_dir: str | Path,
    formats: Iterable[str] | str = FORMATS,
    *,
    blueprint_bytes: bytes | None = None,
) -> tuple[OutputFile, ...]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    selected = _normalize_formats(formats)
    outputs: list[OutputFile] = []
    try:
        if blueprint_bytes is not None:
            blueprint_path = target / "blueprint.yaml"
            blueprint_path.write_bytes(blueprint_bytes)
            outputs.append(_record(blueprint_path, target, "blueprint", None))
        if "sqlite" in selected:
            sqlite_path = target / "data.sqlite3"
            create_sqlite(data, sqlite_path)
            outputs.append(_record(sqlite_path, target, "sqlite", None))
        for table_name in data.table_order:
            table_data = data.tables[table_name]
            if "csv" in selected:
                csv_path = target / "csv" / f"{table_name}.csv"
                _write_csv(table_data, csv_path)
                outputs.append(_record(csv_path, target, "csv", table_name))
            if "jsonl" in selected:
                jsonl_path = target / "jsonl" / f"{table_name}.jsonl"
                _write_jsonl(table_data, jsonl_path)
                outputs.append(_record(jsonl_path, target, "jsonl", table_name))
    except OSError as exc:
        raise ExportError("EXP001", f"cannot write output: {exc}", path=str(target)) from exc
    return tuple(sorted(outputs, key=lambda item: item.path))
