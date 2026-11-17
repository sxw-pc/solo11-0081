from __future__ import annotations

import re
from datetime import datetime

import pytest

from synthlab.errors import GenerationError
from synthlab.generators.boundary import MISSING, boundary_candidate, boundary_hits, matches_boundary
from synthlab.generators.common import derive_seed, make_rng
from synthlab.generators.primitives import GenContext
from synthlab.generators.registry import available_types, create_generator
from synthlab.spec.model import ColumnSpec, ReferenceSpec


def _column(kind: str, name: str = "value", **params) -> ColumnSpec:
    boundary = tuple(params.pop("boundary", ()))
    return ColumnSpec(name=name, type=kind, nullable=params.pop("nullable", True),
                      params=params, boundary=boundary)


def _generate(column: ColumnSpec, count: int = 5, *, parent_values=(), seed: int = 7) -> list:
    generator = create_generator(column, "table")
    values = []
    for row in range(count):
        rng = make_rng(seed, "table", column.name, row)
        values.append(generator.generate(rng, GenContext(row, count, 0, parent_values)))
    return values


def test_registry_covers_all_blueprint_types():
    assert available_types() == (
        "bool", "date", "datetime", "decimal", "enum", "float", "foreign_key",
        "int", "sequence", "string", "text", "uuid",
    )


def test_derived_seeds_are_stable_and_isolated():
    assert derive_seed(1, "a", "b") == derive_seed(1, "a", "b")
    assert derive_seed(1, "a", "b") != derive_seed(1, "a", "c")
    assert derive_seed(1, "a", "b") != derive_seed(2, "a", "b")


def test_int_respects_range_and_step_deterministically():
    column = _column("int", min=10, max=20, step=5)
    first = _generate(column, 40)
    assert first == _generate(column, 40)
    assert set(first) <= {10, 15, 20}
    assert len(set(first)) > 1


def test_float_precision_is_stable():
    values = _generate(_column("float", min=0, max=1, precision=3), 30)
    assert all(round(value, 3) == value for value in values)
    assert all(0 <= value <= 1 for value in values)


def test_decimal_uses_fixed_point_strings():
    values = _generate(_column("decimal", min="1.5", max="2.5", precision=2), 20)
    assert all(re.fullmatch(r"\d+\.\d{2}", value) for value in values)
    assert all("1.50" <= value <= "2.50" for value in values)


def test_string_pattern_and_prefix_lengths():
    pattern_values = _generate(_column("string", pattern="USR-{seq:04d}"), 3)
    assert pattern_values == ["USR-0001", "USR-0002", "USR-0003"]
    prefixed = _generate(_column("string", min_length=5, max_length=8, prefix="AC-", charset="digits"), 20)
    assert all(value.startswith("AC-") and 5 <= len(value) <= 8 for value in prefixed)


def test_text_uses_local_vocabulary_and_word_bounds():
    column = _column("text", min_words=2, max_words=3, words=["amber", "cedar", "river"])
    values = _generate(column, 20)
    assert all(2 <= len(value.rstrip(".").split()) <= 3 for value in values)
    assert all(value[0].isupper() and value.endswith(".") for value in values)


def test_date_and_datetime_stay_in_range():
    dates = _generate(_column("date", start="2024-01-01", end="2024-01-10"), 30)
    assert all("2024-01-01" <= value <= "2024-01-10" for value in dates)
    moments = _generate(
        _column("datetime", start="2024-01-01T00:00:00Z", end="2024-01-02T00:00:00Z", step_seconds=60),
        20,
    )
    assert all(value.endswith("Z") for value in moments)
    assert all(datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset().total_seconds() == 0
               for value in moments)


def test_bool_enum_and_uuid():
    assert all(isinstance(value, bool) for value in _generate(_column("bool", true_weight=0.5), 20))
    enum_column = _column("enum", values={"a": 90, "b": 10})
    values = _generate(enum_column, 3000)
    assert set(values) == {"a", "b"}
    assert 0.07 < values.count("b") / len(values) < 0.13
    uuids = _generate(_column("uuid"), 40)
    assert len(set(uuids)) == 40
    assert all(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", item)
               for item in uuids)


def test_sequence_format_and_foreign_keys():
    values = _generate(_column("sequence", start=10, step=2, format="ID-{value:03d}"), 3)
    assert values == ["ID-010", "ID-012", "ID-014"]
    fk = ColumnSpec(
        name="parent_id", type="foreign_key", nullable=False,
        references=ReferenceSpec("parents", "id"),
    )
    sampled = _generate(fk, 50, parent_values=("P1", "P2", "P3"))
    assert set(sampled) <= {"P1", "P2", "P3"}
    assert len(set(sampled)) > 1


def test_foreign_key_without_parent_values_errors_when_non_nullable():
    fk = ColumnSpec(
        name="parent_id", type="foreign_key", nullable=False,
        references=ReferenceSpec("parents", "id"),
    )
    with pytest.raises(GenerationError) as caught:
        _generate(fk, 1)
    assert caught.value.code == "GEN003"
    assert caught.value.table == "table"
    assert caught.value.column == "parent_id"


def test_boundary_candidates_and_hits():
    integer = _column("int", min=0, max=100, boundary=["min", "max"])
    assert boundary_candidate(integer, "min") == 0
    assert boundary_candidate(integer, "max") == 100
    assert matches_boundary(integer, "min", 0)
    assert not matches_boundary(integer, "min", 1)
    assert boundary_candidate(integer, "empty") is MISSING
    hits = boundary_hits(integer, [50, 100], row_count=2)
    assert hits == {"min": False, "max": True}

    nullable = _column("text", nullable=True, boundary=["null", "empty"], words=["one"])
    assert boundary_candidate(nullable, "null") is None
    assert boundary_candidate(nullable, "empty") == ""
    assert matches_boundary(nullable, "null", None)
    assert matches_boundary(nullable, "empty", "")

    sequence = _column("sequence", start=1, step=2, format="S-{value:02d}")
    assert boundary_candidate(sequence, "max", row_count=3) == "S-05"
    assert matches_boundary(sequence, "max", "S-05", row_count=3)

    date_column = _column("date", start="2024-01-01", end="2024-01-31")
    assert boundary_candidate(date_column, "max") == "2024-01-31"
    assert matches_boundary(date_column, "max", "2024-01-31")
