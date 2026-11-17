from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from synthlab.constraints import check_dataset, effective_nullable
from synthlab.spec.model import ColumnSpec, TableSpec


def _mutate(data, table: str, column: str, row_index: int, value) -> None:
    table_data = data.tables[table]
    index = table_data.column_index(column)
    row = list(table_data.rows[row_index])
    row[index] = value
    table_data.rows[row_index] = tuple(row)


def _codes(report):
    return [issue.code for issue in report.issues]


def test_demo_data_passes_all_constraints(demo_data):
    report = check_dataset(demo_data.dataset, demo_data.tables)
    assert report.ok, [issue.render() for issue in report.issues]
    assert report.boundary["orders"]["note"].coverage == 1.0
    assert report.boundary["products"]["stock_quantity"].hits == {"min": True, "max": True}


def test_primary_key_null_and_duplicate_are_cst001(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "customers", "customer_id", 1, data.tables["customers"].rows[0][0])
    report = check_dataset(data.dataset, data.tables)
    pk_issues = [issue for issue in report.issues if issue.code == "CST001"]
    assert len(pk_issues) == 1
    assert pk_issues[0].table == "customers"
    assert pk_issues[0].column == "customer_id"


def test_missing_foreign_key_is_cst002(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "order_items", "order_id", 0, "ORD-99999")
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST002" and issue.column == "order_id" for issue in report.issues)


def test_non_nullable_null_is_cst003(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "customers", "email", 0, None)
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST003" and issue.column == "email" for issue in report.issues)


def test_invalid_enum_is_cst004(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "customers", "status", 0, "bogus")
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST004" and issue.column == "status" for issue in report.issues)


def test_out_of_range_is_cst005(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "products", "stock_quantity", 0, -1)
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST005" and issue.column == "stock_quantity" for issue in report.issues)


def test_text_word_count_is_cst006(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "customers", "full_name", 0, "solo")
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST006" and issue.column == "full_name" for issue in report.issues)


def test_duplicate_unique_value_is_cst007(demo_data):
    data = deepcopy(demo_data)
    value = data.tables["customers"].rows[0][data.tables["customers"].column_index("email")]
    _mutate(data, "customers", "email", 1, value)
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST007" and issue.column == "email" for issue in report.issues)


def test_table_check_failure_is_cst008(demo_data):
    data = deepcopy(demo_data)
    _mutate(data, "orders", "total_amount", 0, "-5.00")
    report = check_dataset(data.dataset, data.tables)
    assert any(issue.code == "CST008" and issue.column == "total_amount" for issue in report.issues)


def test_required_boundary_coverage_can_fail(demo_data):
    data = deepcopy(demo_data)
    for index in range(data.tables["products"].row_count):
        _mutate(data, "products", "stock_quantity", index, 7)
    strict = replace(data.dataset, require_boundary_coverage=True)
    report = check_dataset(strict, data.tables)
    assert any(issue.code == "CST009" and issue.column == "stock_quantity" for issue in report.issues)
    coverage = report.boundary["products"]["stock_quantity"]
    assert coverage.coverage == 0.0
    assert set(coverage.missing) == {"min", "max"}
    loose = check_dataset(data.dataset, data.tables, enforce_boundary=False)
    assert not any(issue.code == "CST009" for issue in loose.issues)
def test_primary_key_is_never_nullable():
    table = TableSpec(
        "items", 1, "id",
        (ColumnSpec("id", "sequence", nullable=True), ColumnSpec("label", "string", nullable=True)),
    )
    assert not effective_nullable(table, table.column("id"))
    assert effective_nullable(table, table.column("label"))
