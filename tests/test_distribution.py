from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from synthlab.constraints import check_dataset
from synthlab.engine import generate_dataset
from synthlab.errors import GenerationError, SpecValidationError
from synthlab.exporters import export_dataset
from synthlab.manifest import (
    build_manifest,
    compute_fingerprint,
    table_data_sha256,
    table_digest,
    write_manifest,
)
from synthlab.planner import plan_dataset
from synthlab.readers import read_output_tables
from synthlab.spec.loader import load_dataset
from synthlab.spec.model import ColumnSpec, DatasetSpec, DistributionSpec, ReferenceSpec, TableSpec
from synthlab.verifier import verify_dataset

ROOT = Path(__file__).resolve().parents[1]
EASTWEST = ROOT / "fixtures" / "distribution" / "eastwest.yaml"
EASTWEST_CAPACITY = ROOT / "fixtures" / "distribution" / "eastwest_capacity.yaml"


def _class_counts(data, child="orders", fk="customer_id", parent="customers", attribute="region"):
    parent_data = data.table(parent)
    key_class = dict(zip(parent_data.values("customer_id"), parent_data.values(attribute)))
    return Counter(
        key_class[value]
        for value in data.table(child).values(fk)
        if value is not None
    )


def _generate(path):
    spec = load_dataset(path)
    return spec, generate_dataset(spec)


def _export(spec, data, out: Path, formats):
    outputs = export_dataset(data, out, formats, blueprint_bytes=spec.blueprint_bytes)
    write_manifest(out / "manifest.json", build_manifest(data, outputs))
    return out


def _refresh_manifest(out: Path, spec) -> None:
    """Recompute every hash a tamperer would fix up after editing data files."""
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    tables, errors = read_output_tables(out, manifest, spec)
    assert not errors, errors
    for entry in manifest["tables"]:
        entry["data_sha256"] = table_digest(tables[entry["name"]])["data_sha256"]
    for record in manifest["outputs"]:
        payload = (out / record["path"]).read_bytes()
        record["bytes"] = len(payload)
        record["sha256"] = hashlib.sha256(payload).hexdigest()
    digests = [table_digest(tables[name]) for name in tables]
    manifest["content_fingerprint"] = compute_fingerprint(spec, digests, manifest["outputs"])
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _failed_codes(result):
    return {check.code for check in result.checks if check.status == "fail"}


SCENARIO = """
dataset: dist_test
seed: 12
version: "1"
tables:
  - name: customers
    rows: 4
    primary_key: customer_id
    columns:
      - {{name: customer_id, type: sequence, nullable: false, start: 1, format: "CUST-{{value:03d}}"}}
      - {{name: region, type: enum, nullable: false, values: [east, west]}}
  - name: orders
    rows: {rows}
    primary_key: order_id
    columns:
      - {{name: order_id, type: sequence, nullable: false, start: 1}}
      - name: customer_id
        type: foreign_key
        nullable: {nullable}
        {unique_line}
        {boundary_line}
        references: customers.customer_id
        distribute: {distribute}
"""
# seed 12 customers: CUST-001 east, CUST-002 west, CUST-003 east, CUST-004 east


def _scenario(write_yaml, rows=4, nullable="false", unique_line="", boundary_line="",
              distribute="{by: region, quotas: {east: 3, west: 1}, unlisted: forbid}"):
    text = SCENARIO.format(rows=rows, nullable=nullable, unique_line=unique_line,
                           boundary_line=boundary_line, distribute=distribute)
    return load_dataset(write_yaml(text))


def test_exact_quotas_are_met_and_classes_come_from_parent_data():
    spec, data = _generate(EASTWEST)
    assert _class_counts(data) == {"east": 5, "west": 2}
    allowed = set(data.table("customers").values("customer_id"))
    assert set(data.table("orders").values("customer_id")) <= allowed
    report = check_dataset(spec, data.tables)
    assert report.ok, [issue.render() for issue in report.issues]
    info = data.distributions["orders.customer_id"]
    assert info["route"] == "hash"
    assert info["resolved"] == {"east": 5, "west": 2}


def test_same_blueprint_and_seed_produce_identical_fingerprints(tmp_path):
    spec, first = _generate(EASTWEST)
    _, second = _generate(EASTWEST)
    for name in first.table_order:
        assert table_data_sha256(first.table(name)) == table_data_sha256(second.table(name))
    out_a = _export(spec, first, tmp_path / "a", ("sqlite", "csv", "jsonl"))
    out_b = _export(spec, second, tmp_path / "b", ("sqlite", "csv", "jsonl"))
    fingerprint_a = json.loads((out_a / "manifest.json").read_text())["content_fingerprint"]
    fingerprint_b = json.loads((out_b / "manifest.json").read_text())["content_fingerprint"]
    assert fingerprint_a == fingerprint_b


def test_hash_and_capacity_routes_meet_quota_but_assign_different_keys():
    _, hash_data = _generate(EASTWEST)
    _, capacity_data = _generate(EASTWEST_CAPACITY)
    assert _class_counts(hash_data) == {"east": 5, "west": 2}
    assert _class_counts(capacity_data) == {"east": 5, "west": 2}
    hash_keys = hash_data.table("orders").values("customer_id")
    capacity_keys = capacity_data.table("orders").values("customer_id")
    assert hash_keys != capacity_keys
    # capacity-first fills the first east key (CUST-001) before touching others
    assert capacity_keys.count("CUST-001") == 5


def test_unique_capacity_shortfall_is_gen004(write_yaml):
    spec = _scenario(write_yaml, unique_line="unique: true",
                     distribute="{by: region, quotas: {east: 4, west: 0}, unlisted: forbid}")
    with pytest.raises(GenerationError) as caught:
        generate_dataset(spec)
    assert caught.value.code == "GEN004"
    assert caught.value.table == "orders"
    assert caught.value.column == "customer_id"
    assert "unique capacity 1 per key" in str(caught.value)


def test_unique_capacity_exact_fit_is_feasible(write_yaml):
    spec = _scenario(write_yaml, unique_line="unique: true")
    data = generate_dataset(spec)
    assert _class_counts(data) == {"east": 3, "west": 1}
    keys = data.table("orders").values("customer_id")
    assert len(set(keys)) == len(keys)


def test_missing_attribute_value_is_gen004(write_yaml):
    spec = _scenario(write_yaml, distribute="{by: region, quotas: {east: 3, south: 1}, unlisted: forbid}")
    with pytest.raises(GenerationError) as caught:
        generate_dataset(spec)
    assert caught.value.code == "GEN004"
    assert "south" in str(caught.value)


def test_quota_sum_mismatch_with_forbid_is_spec011(write_yaml):
    with pytest.raises(SpecValidationError) as caught:
        _scenario(write_yaml, distribute="{by: region, quotas: {east: 2, west: 1}, unlisted: forbid}")
    assert "SPEC011" in {issue.code for issue in caught.value.issues}


def test_quota_sum_overflow_with_allow_is_spec011(write_yaml):
    with pytest.raises(SpecValidationError) as caught:
        _scenario(write_yaml, distribute="{by: region, quotas: {east: 4, west: 1}, unlisted: allow}")
    assert "SPEC011" in {issue.code for issue in caught.value.issues}


def test_boundary_forced_value_counts_toward_quota(write_yaml):
    spec = _scenario(
        write_yaml, rows=3, boundary_line="boundary: [min]",
        distribute="{by: customer_id, quotas: {CUST-001: 1, CUST-002: 1, CUST-003: 1}, unlisted: forbid}",
    )
    data = generate_dataset(spec)
    assert data.table("orders").values("customer_id")[0] == "CUST-001"


def test_boundary_forced_unlisted_value_is_gen004(write_yaml):
    spec = _scenario(
        write_yaml, rows=3, boundary_line="boundary: [min]",
        distribute="{by: customer_id, quotas: {CUST-002: 1, CUST-003: 1, CUST-004: 1}, unlisted: forbid}",
    )
    with pytest.raises(GenerationError) as caught:
        generate_dataset(spec)
    assert caught.value.code == "GEN004"
    assert "unlisted" in str(caught.value)


def test_boundary_forced_value_exceeding_quota_is_gen004(write_yaml):
    spec = _scenario(
        write_yaml, rows=3, boundary_line="boundary: [min]",
        distribute="{by: customer_id, quotas: {CUST-001: 0, CUST-002: 2, CUST-003: 1}, unlisted: forbid}",
    )
    with pytest.raises(GenerationError) as caught:
        generate_dataset(spec)
    assert caught.value.code == "GEN004"
    assert "exceed the quota" in str(caught.value)


def test_forced_null_reduces_assignable_rows(write_yaml):
    spec = _scenario(write_yaml, rows=5, nullable="true", boundary_line="boundary: [null]")
    data = generate_dataset(spec)
    keys = data.table("orders").values("customer_id")
    assert keys.count(None) == 1
    assert _class_counts(data) == {"east": 3, "west": 1}
    report = check_dataset(spec, data.tables)
    assert report.ok, [issue.render() for issue in report.issues]


def test_null_rate_with_distribute_is_spec011(write_yaml):
    text = """
dataset: bad_null
seed: 1
version: "1"
tables:
  - name: customers
    rows: 2
    primary_key: customer_id
    columns:
      - {name: customer_id, type: sequence, nullable: false, start: 1}
      - {name: region, type: enum, nullable: false, values: [east, west]}
  - name: orders
    rows: 2
    primary_key: order_id
    columns:
      - {name: order_id, type: sequence, nullable: false, start: 1}
      - name: customer_id
        type: foreign_key
        nullable: true
        null_rate: 0.5
        references: customers.customer_id
        distribute: {by: region, quotas: {east: 2}, unlisted: allow}
"""
    with pytest.raises(SpecValidationError) as caught:
        load_dataset(write_yaml(text))
    assert any(issue.code == "SPEC011" and "null_rate" in issue.message
               for issue in caught.value.issues)


def test_proportions_apportion_exactly(write_yaml):
    spec = _scenario(write_yaml, rows=8,
                     distribute="{by: region, proportions: {east: 0.75, west: 0.25}, unlisted: forbid}")
    data = generate_dataset(spec)
    assert _class_counts(data) == {"east": 6, "west": 2}


def test_proportions_must_sum_to_one_with_forbid(write_yaml):
    with pytest.raises(SpecValidationError) as caught:
        _scenario(write_yaml, rows=8,
                  distribute="{by: region, proportions: {east: 0.5, west: 0.25}, unlisted: forbid}")
    assert any(issue.code == "SPEC011" and "sum to 1" in issue.message
               for issue in caught.value.issues)


def test_unlisted_allow_spreads_remainder_over_unlisted_keys(write_yaml):
    spec = _scenario(write_yaml, rows=5, distribute="{by: region, quotas: {east: 3}, unlisted: allow}")
    data = generate_dataset(spec)
    counts = _class_counts(data)
    assert counts["east"] == 3
    assert counts["west"] == 2  # the only unlisted class absorbs the remainder
    report = check_dataset(spec, data.tables)
    assert report.ok, [issue.render() for issue in report.issues]


def test_composite_unique_capacity_is_enforced(write_yaml):
    text = """
dataset: composite
seed: 11
version: "1"
tables:
  - name: customers
    rows: 2
    primary_key: customer_id
    columns:
      - {name: customer_id, type: sequence, nullable: false, start: 1, format: "CUST-{value:03d}"}
      - {name: region, type: enum, nullable: false, values: [east, west]}
  - name: products
    rows: 2
    primary_key: product_id
    columns:
      - {name: product_id, type: sequence, nullable: false, start: 1, format: "SKU-{value:02d}"}
  - name: lines
    rows: 4
    primary_key: line_id
    columns:
      - {name: line_id, type: sequence, nullable: false, start: 1}
      - name: customer_id
        type: foreign_key
        nullable: false
        references: customers.customer_id
        distribute: {by: customer_id, quotas: {CUST-001: 2, CUST-002: 2}, unlisted: forbid}
      - {name: product_id, type: foreign_key, nullable: false, references: products.product_id}
    unique_constraints:
      - [customer_id, product_id]
"""
    spec = load_dataset(write_yaml(text))
    data = generate_dataset(spec)
    pairs = list(zip(data.table("lines").values("customer_id"), data.table("lines").values("product_id")))
    assert len(pairs) == len(set(pairs))
    assert Counter(data.table("lines").values("customer_id")) == {"CUST-001": 2, "CUST-002": 2}
    over = load_dataset(write_yaml(text.replace("{CUST-001: 2, CUST-002: 2}", "{CUST-001: 3, CUST-002: 1}"),
                                   "over.yaml"))
    with pytest.raises(GenerationError) as caught:
        generate_dataset(over)
    assert caught.value.code == "GEN004"
    assert "unique capacity 2 per key" in str(caught.value)


@pytest.mark.parametrize(
    "distribute, fragment",
    [
        ("{by: region, quotas: {east: 3, west: 1}, proportions: {east: 1.0}}", "either quotas or proportions"),
        ("{by: region}", "requires quotas or proportions"),
        ("{by: region, quotas: {east: 3, west: 1}, route: random}", "route"),
        ("{by: region, quotas: {east: 3, west: 1}, unlisted: ignore}", "unlisted"),
        ("{by: missing_attr, quotas: {east: 3, west: 1}}", "does not exist"),
        ("{by: region, quotas: {east: -1, west: 5}}", "non-negative integer"),
        ("{by: region, proportions: {east: 0.9, west: 0.9}}", "must not sum above 1"),
    ],
)
def test_invalid_declarations_are_spec011(write_yaml, distribute, fragment):
    with pytest.raises(SpecValidationError) as caught:
        _scenario(write_yaml, distribute=distribute)
    matches = [issue for issue in caught.value.issues if issue.code == "SPEC011"]
    assert matches, caught.value.issues
    assert any(fragment in issue.message for issue in matches)


def test_distribute_on_non_foreign_key_is_spec011(write_yaml):
    text = """
dataset: bad_target
seed: 1
version: "1"
tables:
  - name: items
    rows: 2
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - name: label
        type: string
        nullable: false
        distribute: {by: id, quotas: {a: 2}, unlisted: allow}
"""
    with pytest.raises(SpecValidationError) as caught:
        load_dataset(write_yaml(text))
    assert any(issue.code == "SPEC011" and "foreign_key" in issue.message
               for issue in caught.value.issues)


def test_distribute_on_self_reference_is_spec011(write_yaml):
    text = """
dataset: bad_self
seed: 1
version: "1"
tables:
  - name: nodes
    rows: 3
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - {name: kind, type: enum, nullable: false, values: [a, b]}
      - name: parent_id
        type: foreign_key
        nullable: true
        references: nodes.id
        distribute: {by: kind, quotas: {a: 2}, unlisted: allow}
"""
    with pytest.raises(SpecValidationError) as caught:
        load_dataset(write_yaml(text))
    assert any(issue.code == "SPEC011" and "self-referencing" in issue.message
               for issue in caught.value.issues)


def test_non_discrete_parent_attribute_is_spec011(write_yaml):
    text = """
dataset: bad_attr
seed: 1
version: "1"
tables:
  - name: customers
    rows: 2
    primary_key: customer_id
    columns:
      - {name: customer_id, type: sequence, nullable: false, start: 1}
      - {name: score, type: float, nullable: false, min: 0, max: 1}
  - name: orders
    rows: 2
    primary_key: order_id
    columns:
      - {name: order_id, type: sequence, nullable: false, start: 1}
      - name: customer_id
        type: foreign_key
        nullable: false
        references: customers.customer_id
        distribute: {by: score, quotas: {0.5: 2}, unlisted: allow}
"""
    with pytest.raises(SpecValidationError) as caught:
        load_dataset(write_yaml(text))
    assert any(issue.code == "SPEC011" and "discrete" in issue.message
               for issue in caught.value.issues)


def test_planner_records_distribution_edges_and_checks_attribute():
    spec, _ = _generate(EASTWEST)
    plan = plan_dataset(spec)
    assert [(edge.child, edge.column, edge.parent, edge.attribute) for edge in plan.distributions] == [
        ("orders", "customer_id", "customers", "region")
    ]
    assert "orders.customer_id by customers.region" in plan.readable()
    assert plan.as_dict()["distributions"] == [
        {"child": "orders", "column": "customer_id", "parent": "customers", "attribute": "region"}
    ]


def test_planner_rejects_unknown_parent_attribute():
    parent = TableSpec(
        "customers", 2, "customer_id",
        (ColumnSpec("customer_id", "sequence", nullable=False, params={"start": 1}),),
    )
    child = TableSpec(
        "orders", 2, "order_id",
        (
            ColumnSpec("order_id", "sequence", nullable=False, params={"start": 1}),
            ColumnSpec(
                "customer_id", "foreign_key", nullable=False,
                references=ReferenceSpec("customers", "customer_id"),
                distribute=DistributionSpec("region", (("east", 2),), "quotas"),
            ),
        ),
    )
    dataset = DatasetSpec(
        name="prog", seed=1, version="1", tables=(parent, child),
        require_boundary_coverage=False, max_rows=100, source_path="memory.yaml",
        blueprint_sha256="0" * 64,
    )
    from synthlab.errors import SynthlabError

    with pytest.raises(SynthlabError) as caught:
        plan_dataset(dataset)
    assert caught.value.code == "SPEC011"


def test_constraints_recompute_counts_from_data_not_engine():
    spec, data = _generate(EASTWEST)
    orders = data.table("orders")
    customers = data.table("customers")
    region_of = dict(zip(customers.values("customer_id"), customers.values("region")))
    index = orders.column_index("customer_id")

    def swap_to(target_region):
        mutated = [list(row) for row in orders.rows]
        victim = next(i for i, row in enumerate(mutated) if region_of[row[index]] != target_region)
        replacement = next(key for key, region in region_of.items()
                           if region == target_region and key != mutated[victim][index])
        mutated[victim][index] = replacement
        return mutated

    # swapping to another region breaks the quota
    orders.rows = [tuple(row) for row in swap_to(
        "west" if region_of[orders.rows[0][index]] == "east" else "east")]
    report = check_dataset(spec, data.tables)
    assert any(issue.code == "CST010" and issue.column == "customer_id" for issue in report.issues)

    # swapping inside the same region keeps the business distribution valid
    _, fresh = _generate(EASTWEST)
    fresh_orders = fresh.table("orders")
    fresh_region_of = dict(zip(fresh.table("customers").values("customer_id"),
                               fresh.table("customers").values("region")))
    idx = fresh_orders.column_index("customer_id")
    mutated = [list(row) for row in fresh_orders.rows]
    victim = next(i for i, row in enumerate(mutated)
                  if sum(1 for key, region in fresh_region_of.items()
                         if region == fresh_region_of[row[idx]]) > 1
                  and any(key != row[idx] and fresh_region_of[key] == fresh_region_of[row[idx]]
                          for key in fresh_region_of))
    donor = next(key for key in fresh_region_of
                 if key != mutated[victim][idx]
                 and fresh_region_of[key] == fresh_region_of[mutated[victim][idx]])
    mutated[victim][idx] = donor
    fresh_orders.rows = [tuple(row) for row in mutated]
    report = check_dataset(spec, fresh.tables)
    assert report.ok, [issue.render() for issue in report.issues]


def _tamper_target(data):
    customers = data.table("customers")
    region_of = dict(zip(customers.values("customer_id"), customers.values("region")))
    orders = data.table("orders")
    victim = next(i for i, key in enumerate(orders.values("customer_id")))
    other = next(key for key, region in region_of.items()
                 if region != region_of[orders.values("customer_id")[victim]])
    return victim, orders.values("customer_id")[victim], other


@pytest.mark.parametrize("formats", [("sqlite",), ("csv",), ("jsonl",)])
def test_verifier_detects_region_swap_after_reread(tmp_path, formats):
    spec, data = _generate(EASTWEST)
    out = _export(spec, data, tmp_path / formats[0], formats)
    assert verify_dataset(out).ok
    victim, _old, other = _tamper_target(data)
    if formats == ("sqlite",):
        connection = sqlite3.connect(out / "data.sqlite3")
        order_id = data.table("orders").values("order_id")[victim]
        connection.execute("UPDATE orders SET customer_id=? WHERE order_id=?", (other, order_id))
        connection.commit()
        connection.close()
    elif formats == ("csv",):
        path = out / "csv" / "orders.csv"
        rows = list(csv.reader(path.open()))
        column = list(data.table("orders").columns).index("customer_id")
        rows[1 + victim][column] = other
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)
    else:
        path = out / "jsonl" / "orders.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[victim])
        payload["customer_id"] = other
        lines[victim] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _refresh_manifest(out, spec)  # hashes and fingerprint are fixed up by the tamperer
    result = verify_dataset(out)
    assert not result.ok
    assert "VER013" in _failed_codes(result)


def test_verifier_accepts_same_class_key_swap(tmp_path):
    spec, data = _generate(EASTWEST)
    out = _export(spec, data, tmp_path / "out", ("jsonl",))
    customers = data.table("customers")
    region_of = dict(zip(customers.values("customer_id"), customers.values("region")))
    orders = data.table("orders")
    victim = next(
        i for i, key in enumerate(orders.values("customer_id"))
        if any(other != key and region_of[other] == region_of[key] for other in region_of)
    )
    current = orders.values("customer_id")[victim]
    donor = next(key for key in region_of
                 if key != current and region_of[key] == region_of[current])
    path = out / "jsonl" / "orders.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[victim])
    payload["customer_id"] = donor
    lines[victim] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _refresh_manifest(out, spec)
    result = verify_dataset(out)
    assert result.ok, [check.message for check in result.checks if check.status == "fail"]


def test_manifest_records_route_and_verifier_crosschecks_it(tmp_path):
    spec, data = _generate(EASTWEST)
    out = _export(spec, data, tmp_path / "out", ("csv",))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["distributions"] == [
        {
            "table": "orders",
            "column": "customer_id",
            "parent": "customers",
            "parent_column": "customer_id",
            "by": "region",
            "mode": "quotas",
            "route": "hash",
            "unlisted": "forbid",
            "declared": {"east": 5, "west": 2},
            "resolved": {"east": 5, "west": 2},
            "remainder": 0,
            "assignable_rows": 7,
        }
    ]
    result = verify_dataset(out)
    assert any(check.id == "distributions" and check.status == "pass" for check in result.checks)
    # a tampered route declaration is caught even though data is untouched
    manifest["distributions"][0]["route"] = "capacity"
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = verify_dataset(out)
    assert "VER010" in _failed_codes(result)


def test_generate_and_verify_cli_roundtrip(tmp_path, capsys):
    from synthlab.cli import main

    out = tmp_path / "dist"
    assert main(["generate", "--spec", str(EASTWEST), "--out", str(out)]) == 0
    assert main(["verify", "--out", str(out)]) == 0
    assert "verification: pass" in capsys.readouterr().out


def test_infeasible_blueprint_exports_nothing(tmp_path, capsys):
    from synthlab.cli import main

    spec = _scenario(
        lambda text, name="dataset.yaml": _write(tmp_path, text, name),
        unique_line="unique: true",
        distribute="{by: region, quotas: {east: 4, west: 0}, unlisted: forbid}",
    )
    out = tmp_path / "out"
    assert main(["generate", "--spec", str(spec.source_path), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert "GEN004" in captured.err
    assert not (out / "manifest.json").exists()


def _write(directory: Path, text: str, name: str) -> Path:
    target = directory / name
    target.write_text(text, encoding="utf-8")
    return target
