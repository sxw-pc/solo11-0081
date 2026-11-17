"""SQLite materialization with real schema constraints."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .constraints import effective_nullable
from .errors import ExportError
from .spec.model import ColumnSpec, DatasetData, DatasetSpec, TableData, TableSpec

SQL_TYPES = {
    "int": "INTEGER",
    "float": "REAL",
    "decimal": "TEXT",
    "string": "TEXT",
    "text": "TEXT",
    "date": "TEXT",
    "datetime": "TEXT",
    "bool": "INTEGER",
    "enum": "TEXT",
    "uuid": "TEXT",
    "sequence": "TEXT",
}


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parent_column_type(dataset: DatasetSpec, column: ColumnSpec) -> str:
    if column.references is None:
        return "TEXT"
    parent = dataset.table(column.references.table)
    if parent is None:
        return "TEXT"
    target = parent.column(column.references.column)
    if target is None or target.type == "foreign_key":
        return "TEXT"
    return SQL_TYPES.get(target.type, "TEXT")


def sql_type(dataset: DatasetSpec, column: ColumnSpec) -> str:
    if column.type == "foreign_key":
        return _parent_column_type(dataset, column)
    return SQL_TYPES.get(column.type, "TEXT")


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _column_clause(dataset: DatasetSpec, table: TableSpec, column: ColumnSpec) -> str:
    clauses = [quote_identifier(column.name), sql_type(dataset, column)]
    if column.name == table.primary_key:
        clauses.append("PRIMARY KEY")
    if not effective_nullable(table, column):
        clauses.append("NOT NULL")
    if column.unique:
        clauses.append("UNIQUE")
    return " ".join(clauses)


def _create_table_sql(dataset: DatasetSpec, table: TableSpec) -> str:
    clauses = [_column_clause(dataset, table, column) for column in table.columns]
    for group in table.unique_constraints:
        clauses.append("UNIQUE (" + ", ".join(quote_identifier(name) for name in group) + ")")
    for column in table.columns:
        if column.references is not None:
            clauses.append(
                "FOREIGN KEY (" + quote_identifier(column.name) + ") REFERENCES "
                + quote_identifier(column.references.table)
                + " (" + quote_identifier(column.references.column) + ")"
            )
    for check in table.checks:
        clauses.append(
            f"CHECK ({quote_identifier(check.column)} {check.operator} {_sql_literal(check.value)})"
        )
    return f"CREATE TABLE {quote_identifier(table.name)} (\n  " + ",\n  ".join(clauses) + "\n)"


def _insert_rows(connection: sqlite3.Connection, table: TableSpec, data: TableData) -> None:
    placeholders = ", ".join("?" for _ in table.columns)
    columns = ", ".join(quote_identifier(name) for name in table.column_names)
    sql = f"INSERT INTO {quote_identifier(table.name)} ({columns}) VALUES ({placeholders})"
    values = [tuple(row) for row in data.rows]
    if values:
        connection.executemany(sql, values)


def create_sqlite(data: DatasetData, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(target)
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            for name in data.table_order:
                table = data.dataset.table(name)
                if table is None:
                    raise ExportError("EXP001", f"missing table spec for {name}", table=name)
                connection.execute(_create_table_sql(data.dataset, table))
            for name in data.table_order:
                table = data.dataset.table(name)
                if table is None:
                    continue
                _insert_rows(connection, table, data.tables[name])
            for name in data.table_order:
                table = data.dataset.table(name)
                if table is None:
                    continue
                for column in table.columns:
                    if column.references is not None:
                        index_name = f"idx_{name}_{column.name}"
                        connection.execute(
                            f"CREATE INDEX {quote_identifier(index_name)} "
                            f"ON {quote_identifier(name)} ({quote_identifier(column.name)})"
                        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ExportError("EXP002", f"SQLite foreign key check failed: {violations[:3]!r}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ExportError("EXP002", f"SQLite integrity check failed: {integrity!r}")
    except sqlite3.Error as exc:
        raise ExportError("EXP001", f"SQLite export failed: {exc}", path=str(target)) from exc
    finally:
        if connection is not None:
            connection.close()
