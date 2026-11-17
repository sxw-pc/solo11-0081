from __future__ import annotations

import pytest

from synthlab.errors import SpecValidationError
from synthlab.spec.loader import load_blueprint
from synthlab.spec.validator import collect_issues, validate_blueprint

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _codes(issues):
    return {issue.code for issue in issues}


def _minimal(column: str = "{name: id, type: sequence, nullable: false, start: 1}",
             extra_table: str = "", rows: int = 2) -> str:
    return f"""
dataset: test_data
seed: 42
version: "1.0.0"
tables:
  - name: items
    rows: {rows}
    primary_key: id
    columns:
      - {column}
{extra_table}
"""


def test_demo_blueprint_has_no_issues():
    issues = collect_issues(load_blueprint(ROOT / "fixtures" / "demo" / "dataset.yaml"))
    assert issues == []


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("missing_table.yaml", "SPEC004"),
        ("bad_reference.yaml", "SPEC004"),
        ("cyclic_reference.yaml", "SPEC005"),
    ],
)
def test_invalid_fixtures_have_expected_codes(fixture, code):
    blueprint = load_blueprint(ROOT / "fixtures" / "invalid" / fixture)
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(blueprint)
    assert code in _codes(caught.value.issues)
    assert str(ROOT / "fixtures" / "invalid" / fixture) in str(caught.value)


def test_unknown_type_is_spec002(write_yaml):
    path = write_yaml(_minimal("{name: id, type: mystery, nullable: false, start: 1}"))
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(path))
    issue = next(item for item in caught.value.issues if item.code == "SPEC002")
    assert issue.table == "items"
    assert issue.column == "id"
    assert "mystery" in issue.message


def test_duplicate_table_and_column_are_spec003(write_yaml):
    text = """
dataset: dup
seed: 1
version: "1"
tables:
  - name: items
    rows: 1
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - {name: id, type: int, min: 0, max: 2}
  - name: items
    rows: 1
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
"""
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    assert "SPEC003" in _codes(caught.value.issues)


def test_null_rate_and_max_rows_are_spec006(write_yaml):
    text = _minimal("{name: id, type: int, min: 0, max: 5, nullable: false, null_rate: 0.2}")
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    assert "SPEC006" in _codes(caught.value.issues)
    too_many = """
dataset: rows
seed: 1
version: "1"
max_rows: 5
tables:
  - name: items
    rows: 6
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
"""
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(too_many, "rows.yaml")))
    assert "SPEC006" in _codes(caught.value.issues)


def test_enum_weights_are_spec007(write_yaml):
    column = """
{name: id, type: enum, nullable: false, values: [{value: one, weight: 0}, {value: two, weight: -1}]}
""".strip()
    path = write_yaml(_minimal(column))
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(path))
    assert "SPEC007" in _codes(caught.value.issues)


def test_duplicate_unique_constraint_is_spec008(write_yaml):
    text = _minimal(extra_table="    unique_constraints:\n      - [id]\n      - [id]\n")
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    assert "SPEC008" in _codes(caught.value.issues)


def test_missing_required_fields_are_spec001(write_yaml):
    path = write_yaml("dataset: broken\nseed: 1\nversion: '1'\n")
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(path))
    assert "SPEC001" in _codes(caught.value.issues)


def test_reference_must_target_unique_column(write_yaml):
    text = """
dataset: refs
seed: 1
version: "1"
tables:
  - name: parents
    rows: 2
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - {name: label, type: string, nullable: false, pattern: "P-{seq}"}
  - name: children
    rows: 2
    primary_key: id
    columns:
      - {name: id, type: sequence, nullable: false, start: 1}
      - {name: parent_label, type: foreign_key, nullable: false, references: parents.label}
"""
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    codes = [issue.code for issue in caught.value.issues]
    assert "SPEC004" in codes


def test_self_reference_must_be_nullable(write_yaml):
    text = _minimal(
        "{name: id, type: sequence, nullable: false, start: 1}",
        "      - {name: parent_id, type: foreign_key, nullable: false, references: items.id}\n",
    )
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    assert any(issue.code == "SPEC004" and issue.column == "parent_id" for issue in caught.value.issues)


def test_boundary_markers_are_validated(write_yaml):
    text = _minimal("{name: id, type: int, min: 0, max: 2, nullable: false, boundary: [null, empty]}")
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    boundary_issues = [issue for issue in caught.value.issues if issue.code == "SPEC009"]
    assert boundary_issues


def test_multiple_errors_are_aggregated(write_yaml):
    text = """
dataset: errors
seed: nope
version: "1"
tables:
  - name: items
    rows: 1
    primary_key: missing
    columns:
      - {name: id, type: unknown, nullable: false, start: 1}
"""
    with pytest.raises(SpecValidationError) as caught:
        validate_blueprint(load_blueprint(write_yaml(text)))
    assert len(caught.value.issues) >= 3
    assert "items" in str(caught.value)
    rendered = str(caught.value)
    assert "SPEC001" in rendered and "SPEC002" in rendered
