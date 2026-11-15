from __future__ import annotations

import csv
import hashlib
import json
import sqlite3

import pytest

from synthlab.exporters import NULL_TOKEN, export_dataset
from synthlab.errors import ExportError


def test_export_all_formats_produces_expected_files(demo_data, tmp_path):
    outputs = export_dataset(
        demo_data, tmp_path, "sqlite,csv,jsonl", blueprint_bytes=demo_data.dataset.blueprint_bytes
    )
    paths = {record.path for record in outputs}
    assert "blueprint.yaml" in paths
    assert "data.sqlite3" in paths
    for table in demo_data.table_order:
        assert f"csv/{table}.csv" in paths
        assert f"jsonl/{table}.jsonl" in paths
    assert len(outputs) == 14
    for record in outputs:
        path = tmp_path / record.path
        payload = path.read_bytes()
        assert record.bytes == len(payload)
        assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "blueprint.yaml").read_bytes() == demo_data.dataset.blueprint_bytes


def test_csv_is_stable_utf8_and_uses_explicit_null_token(demo_data, tmp_path):
    export_dataset(demo_data, tmp_path, "csv")
    content = (tmp_path / "csv" / "orders.csv").read_bytes()
    assert b"\r\n" not in content
    with (tmp_path / "csv" / "orders.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(demo_data.table("orders").columns)
    assert len(rows) == demo_data.table("orders").row_count + 1
    note_index = rows[0].index("note")
    notes = [row[note_index] for row in rows[1:]]
    assert NULL_TOKEN in notes
    assert "" in notes


def test_jsonl_has_one_object_per_row_and_ordered_keys(demo_data, tmp_path):
    export_dataset(demo_data, tmp_path, "jsonl")
    lines = (tmp_path / "jsonl" / "customers.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == demo_data.table("customers").row_count
    payload = json.loads(lines[0])
    assert list(payload) == list(demo_data.table("customers").columns)
    assert isinstance(payload["is_active"], bool)
    assert payload["external_id"].count("-") == 4
    assert b"\r\n" not in (tmp_path / "jsonl" / "customers.jsonl").read_bytes()


def test_sqlite_has_schema_constraints_and_rows(demo_data, tmp_path):
    export_dataset(demo_data, tmp_path, "sqlite")
    connection = sqlite3.connect(tmp_path / "data.sqlite3")
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in demo_data.table_order
        }
        assert counts == {name: demo_data.table(name).row_count for name in demo_data.table_order}
        schemas = {
            row[0]: row[1]
            for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        }
        customer_sql = schemas["customers"]
        assert "PRIMARY KEY" in customer_sql
        assert "UNIQUE" in customer_sql
        assert "NOT NULL" in customer_sql
        order_items_sql = schemas["order_items"]
        assert "FOREIGN KEY" in order_items_sql
        assert "UNIQUE" in order_items_sql
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        assert "idx_order_items_order_id" in indexes
        value = connection.execute(
            'SELECT note FROM orders WHERE note IS NULL LIMIT 1'
        ).fetchone()
        assert value == (None,)
    finally:
        connection.close()
def test_unknown_format_is_exp003(demo_data, tmp_path):
    with pytest.raises(ExportError) as caught:
        export_dataset(demo_data, tmp_path, "parquet")
    assert caught.value.code == "EXP003"
    assert "parquet" in str(caught.value)


def test_export_bytes_are_repeatable(demo_data, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    one = export_dataset(demo_data, first, "csv,jsonl", blueprint_bytes=demo_data.dataset.blueprint_bytes)
    two = export_dataset(demo_data, second, "csv,jsonl", blueprint_bytes=demo_data.dataset.blueprint_bytes)
    assert [(item.path, item.sha256) for item in one] == [(item.path, item.sha256) for item in two]
    assert (first / "csv" / "customers.csv").read_bytes() == (second / "csv" / "customers.csv").read_bytes()
