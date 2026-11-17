from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from synthlab.errors import SynthlabError
from synthlab.spec.loader import load_blueprint, load_dataset, parse_check

ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = ROOT / "fixtures" / "demo" / "dataset.yaml"


def test_load_blueprint_records_hash_and_source():
    blueprint = load_blueprint(DEMO_YAML)
    assert blueprint.sha256 == hashlib.sha256(blueprint.raw_bytes).hexdigest()
    assert blueprint.data["dataset"] == "demo_shop"
    assert Path(blueprint.path) == DEMO_YAML


def test_load_dataset_normalizes_model():
    spec = load_dataset(DEMO_YAML)
    assert spec.name == "demo_shop"
    assert spec.seed == 20251115
    assert spec.version == "1.0.0"
    assert tuple(spec.table_names) == (
        "categories", "regions", "customers", "products", "orders", "order_items"
    )
    categories = spec.table("categories")
    assert categories.primary_key == "category_id"
    parent = categories.column("parent_id")
    assert parent.references.table == "categories"
    assert parent.boundary == ("null",)
    orders = spec.table("orders")
    assert orders.checks[0].column == "total_amount"
    assert orders.checks[0].operator == ">="
    assert spec.table("order_items").unique_constraints == (("order_id", "product_id"),)
    assert spec.blueprint_sha256 == hashlib.sha256(spec.blueprint_bytes).hexdigest()


def test_parse_check_supports_strings_and_mappings():
    assert parse_check("amount >= 10").value == 10
    assert parse_check("status == 'active'").value == "active"
    check = parse_check({"column": "qty", "op": "<=", "value": 3})
    assert (check.column, check.operator, check.value) == ("qty", "<=", 3)
    with pytest.raises(ValueError):
        parse_check("not a check")


def test_missing_file_has_io_code(tmp_path):
    with pytest.raises(SynthlabError) as caught:
        load_blueprint(tmp_path / "absent.yaml")
    assert caught.value.code == "IO001"
    assert "absent.yaml" in str(caught.value)


def test_duplicate_yaml_key_is_rejected(write_yaml):
    path = write_yaml(
        """
dataset: duplicate
seed: 1
version: "1"
tables:
  - name: t
    rows: 1
    rows: 2
    primary_key: id
    columns:
      - {name: id, type: sequence, start: 1}
"""
    )
    with pytest.raises(SynthlabError) as caught:
        load_blueprint(path)
    assert caught.value.code == "SPEC003"
    assert "rows" in str(caught.value)


def test_non_mapping_root_is_spec000(write_yaml):
    path = write_yaml("- one\n- two\n")
    with pytest.raises(SynthlabError) as caught:
        load_blueprint(path)
    assert caught.value.code == "SPEC000"
    assert str(path) in str(caught.value)


def test_invalid_yaml_is_spec000(write_yaml):
    path = write_yaml("dataset: [unclosed\n")
    with pytest.raises(SynthlabError) as caught:
        load_blueprint(path)
    assert caught.value.code == "SPEC000"
    assert "invalid YAML" in str(caught.value)


def test_load_dataset_validates_by_default():
    with pytest.raises(SynthlabError) as caught:
        load_dataset(ROOT / "fixtures" / "invalid" / "missing_table.yaml")
    assert caught.value.code == "SPEC004"
    assert caught.value.table == "children"
    assert caught.value.column == "parent_id"

