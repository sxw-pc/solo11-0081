"""Read exported data back without any generation-engine state."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .exporters import NULL_TOKEN
from .spec.model import ColumnSpec, DatasetSpec, TableData, TableSpec
from .sqlite_export import quote_identifier


class OutputReadError(Exception):
    pass


def safe_output_path(out_dir: str | Path, relative: str) -> Path:
    root = Path(out_dir).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise OutputReadError(f"output path escapes the output directory: {relative}") from exc
    return candidate


def _target_type(dataset: DatasetSpec, column: ColumnSpec) -> str:
    if column.type != "foreign_key" or column.references is None:
        return column.type
    parent = dataset.table(column.references.table)
    target = parent.column(column.references.column) if parent else None
    return target.type if target else "string"


def convert_text(dataset: DatasetSpec, column: ColumnSpec, text: str) -> Any:
    if text == NULL_TOKEN:
        return None
    kind = _target_type(dataset, column)
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "bool":
        lowered = text.lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise OutputReadError(f"invalid boolean value {text!r}")
    return text


def convert_sqlite(dataset: DatasetSpec, column: ColumnSpec, value: Any) -> Any:
    if value is None:
        return None
    kind = _target_type(dataset, column)
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def load_jsonl(path: Path, table: TableSpec) -> TableData:
    rows: list[tuple[Any, ...]] = []
    expected = set(table.column_names)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise OutputReadError(f"{path.name}: empty line at {line_number}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OutputReadError(f"{path.name}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(payload, dict) or set(payload) != expected:
                raise OutputReadError(f"{path.name}:{line_number}: columns do not match blueprint")
            rows.append(tuple(payload[name] for name in table.column_names))
    return TableData(table.name, table.column_names, rows, table)


def load_csv(dataset: DatasetSpec, path: Path, table: TableSpec) -> TableData:
    rows: list[tuple[Any, ...]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise OutputReadError(f"{path.name}: empty CSV file") from exc
        if tuple(header) != table.column_names:
            raise OutputReadError(f"{path.name}: header does not match blueprint column order")
        for line_number, record in enumerate(reader, start=2):
            if len(record) != len(table.columns):
                raise OutputReadError(f"{path.name}:{line_number}: wrong number of fields")
            row: list[Any] = []
            for column, text in zip(table.columns, record):
                try:
                    row.append(convert_text(dataset, column, text))
                except (ValueError, OutputReadError) as exc:
                    raise OutputReadError(f"{path.name}:{line_number}:{column.name}: {exc}") from exc
            rows.append(tuple(row))
    return TableData(table.name, table.column_names, rows, table)


def load_sqlite(dataset: DatasetSpec, path: Path, table: TableSpec) -> TableData:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        columns = ", ".join(quote_identifier(name) for name in table.column_names)
        raw_rows = connection.execute(f"SELECT {columns} FROM {quote_identifier(table.name)}").fetchall()
    except sqlite3.Error as exc:
        raise OutputReadError(f"cannot read SQLite table {table.name}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    rows = [
        tuple(convert_sqlite(dataset, column, value) for column, value in zip(table.columns, raw))
        for raw in raw_rows
    ]
    return TableData(table.name, table.column_names, rows, table)


def _table_source(
    out_dir: Path, manifest: dict[str, Any], table_name: str
) -> tuple[str, Path] | None:
    outputs = manifest.get("outputs", [])
    for format_name in ("jsonl", "csv"):
        for item in outputs:
            if item.get("format") == format_name and item.get("table") == table_name:
                return format_name, safe_output_path(out_dir, str(item["path"]))
    for item in outputs:
        if item.get("format") == "sqlite":
            return "sqlite", safe_output_path(out_dir, str(item["path"]))
    return None


def read_output_tables(
    out_dir: str | Path,
    manifest: dict[str, Any],
    dataset: DatasetSpec,
) -> tuple[dict[str, TableData], dict[str, str]]:
    target = Path(out_dir)
    tables: dict[str, TableData] = {}
    errors: dict[str, str] = {}
    for spec in dataset.tables:
        source = _table_source(target, manifest, spec.name)
        if source is None:
            errors[spec.name] = "no exported table file found in manifest"
            continue
        format_name, path = source
        try:
            if not path.is_file():
                raise OutputReadError(f"exported file does not exist: {path.relative_to(target)}")
            if format_name == "jsonl":
                tables[spec.name] = load_jsonl(path, spec)
            elif format_name == "csv":
                tables[spec.name] = load_csv(dataset, path, spec)
            else:
                tables[spec.name] = load_sqlite(dataset, path, spec)
        except (OSError, OutputReadError, ValueError) as exc:
            errors[spec.name] = str(exc)
    return tables, errors
