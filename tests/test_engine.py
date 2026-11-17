from __future__ import annotations

from dataclasses import replace

import pytest

from synthlab.engine import generate_dataset
from synthlab.errors import GenerationError
from synthlab.manifest import table_data_sha256
from synthlab.planner import plan_dataset
from synthlab.spec.loader import load_dataset


def test_generation_is_deterministic(demo_spec):
    first = generate_dataset(demo_spec)
    second = generate_dataset(demo_spec)
    assert first.table_order == second.table_order
    for name in first.table_order:
        assert first.tables[name].rows == second.tables[name].rows
        assert table_data_sha256(first.tables[name]) == table_data_sha256(second.tables[name])


def test_different_seed_changes_content(demo_spec):
    first = generate_dataset(demo_spec)
    other = generate_dataset(replace(demo_spec, seed=demo_spec.seed + 1))
    assert table_data_sha256(first.table("customers")) != table_data_sha256(other.table("customers"))


def test_rows_columns_and_plan_order(demo_data):
    assert demo_data.table_order == ("categories", "regions", "customers", "products", "orders", "order_items")
    for name in demo_data.table_order:
        spec = demo_data.dataset.table(name)
        data = demo_data.table(name)
        assert data.row_count == spec.rows
        assert data.columns == spec.column_names
        assert all(len(row) == len(spec.columns) for row in data.rows)


def test_primary_and_foreign_keys_reference_real_parents(demo_data):
    for table in demo_data.dataset.tables:
        data = demo_data.table(table.name)
        keys = data.values(table.primary_key)
        assert None not in keys
        assert len(keys) == len(set(keys))
        for column in table.columns:
            if column.references is None:
                continue
            parent = demo_data.table(column.references.table)
            allowed = set(parent.values(column.references.column))
            assert {value for value in data.values(column.name) if value is not None} <= allowed


def test_self_references_only_point_to_earlier_rows(demo_data):
    categories = demo_data.table("categories")
    ids = categories.values("category_id")
    parents = categories.values("parent_id")
    assert parents[0] is None
    for index, parent in enumerate(parents):
        if parent is not None:
            assert parent in set(ids[:index])


def test_unique_constraints_hold(demo_data):
    customers = demo_data.table("customers")
    assert len(set(customers.values("email"))) == customers.row_count
    order_items = demo_data.table("order_items")
    pairs = list(zip(order_items.values("order_id"), order_items.values("product_id")))
    assert len(pairs) == len(set(pairs))


def test_null_rates_and_boundary_injection(demo_data):
    customers = demo_data.table("customers")
    assert all(value is not None for value in customers.values("email"))
    assert any(value is None for value in customers.values("bio"))
    notes = demo_data.table("orders").values("note")
    assert None in notes
    assert "" in notes
    stock = demo_data.table("products").values("stock_quantity")
    assert 0 in stock and 500 in stock
    loyalty = customers.values("loyalty_score")
    assert 0 in loyalty and 100 in loyalty
    created = demo_data.table("categories").values("created_on")
    assert "2022-01-01" in created and "2024-12-31" in created


def test_generation_stats_capture_nulls_distincts_and_boundaries(demo_data):
    stats = demo_data.column_stats["orders"]["note"]
    assert stats["null_count"] >= 1
    assert stats["distinct_count"] > 1
    assert stats["boundary_hits"] == {"null": True, "empty": True}
    assert demo_data.column_stats["customers"]["email"]["null_count"] == 0
    assert demo_data.column_stats["customers"]["email"]["distinct_count"] == 80


def test_explicit_plan_is_reused(demo_spec):
    plan = plan_dataset(demo_spec)
    data = generate_dataset(demo_spec, plan)
    assert data.table_order == plan.order


def test_unique_retry_failure_is_gen001(write_yaml):
    path = write_yaml(
        """
dataset: impossible_unique
seed: 3
version: "1"
tables:
  - name: items
    rows: 3
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - {name: code, type: enum, nullable: false, unique: true, values: [only]}
"""
    )
    spec = load_dataset(path)
    with pytest.raises(GenerationError) as caught:
        generate_dataset(spec)
    assert caught.value.code == "GEN001"
    assert caught.value.table == "items"
    assert caught.value.column == "code"
