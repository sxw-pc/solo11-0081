from __future__ import annotations

import pytest

from synthlab.errors import SynthlabError
from synthlab.planner import plan_dataset
from synthlab.spec.model import ColumnSpec, DatasetSpec, ReferenceSpec, TableSpec


def _table(name: str, references: list[tuple[str, str]] | None = None,
           self_reference: bool = False) -> TableSpec:
    columns = [ColumnSpec("id", "sequence", nullable=False, params={"start": 1})]
    for child_column, target in references or []:
        columns.append(ColumnSpec(
            child_column,
            "foreign_key",
            nullable=True,
            references=ReferenceSpec(target, "id"),
        ))
    if self_reference:
        columns.append(ColumnSpec(
            "parent_id", "foreign_key", nullable=True,
            references=ReferenceSpec(name, "id"),
        ))
    return TableSpec(name, 2, "id", tuple(columns))


def _dataset(tables: list[TableSpec]) -> DatasetSpec:
    return DatasetSpec(
        name="plan_test", seed=1, version="1", tables=tuple(tables), require_boundary_coverage=False,
        max_rows=1000, source_path="memory.yaml", blueprint_sha256="0" * 64,
    )


def test_topological_order_respects_dependencies_and_sorts_layers():
    spec = _dataset([
        _table("child", [("mid_id", "mid"), ("root_id", "root_a")]),
        _table("mid", [("root_id", "root_b")]),
        _table("root_b"),
        _table("root_a"),
    ])
    plan = plan_dataset(spec)
    assert plan.order == ("root_a", "root_b", "mid", "child")
    assert [(edge.child, edge.parent) for edge in plan.edges] == [
        ("child", "mid"), ("child", "root_a"), ("mid", "root_b")
    ]


def test_self_reference_is_recorded_but_not_a_cycle():
    spec = _dataset([_table("nodes", self_reference=True)])
    plan = plan_dataset(spec)
    assert plan.order == ("nodes",)
    assert plan.self_references == (("nodes", "parent_id"),)
    assert "Self references" in plan.readable()
    assert plan.as_dict()["self_references"] == [{"table": "nodes", "column": "parent_id"}]


def test_cycle_has_spec005():
    spec = _dataset([
        _table("alpha", [("beta_id", "beta")]),
        _table("beta", [("alpha_id", "alpha")]),
    ])
    with pytest.raises(SynthlabError) as caught:
        plan_dataset(spec)
    assert caught.value.code == "SPEC005"
    assert "alpha" in str(caught.value)
    assert "beta" in str(caught.value)


def test_plan_text_contains_order_edges_and_self_reference():
    spec = _dataset([
        _table("parent"),
        _table("child", [("parent_id", "parent")]),
        _table("nodes", self_reference=True),
    ])
    rendered = plan_dataset(spec).readable()
    assert "Execution plan:" in rendered
    assert "child.parent_id -> parent.id" in rendered
    assert "nodes.parent_id -> nodes" in rendered
