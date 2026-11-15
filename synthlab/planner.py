"""Stable table dependency planning."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import SynthlabError
from .spec.model import DatasetSpec


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    child: str
    column: str
    parent: str
    parent_column: str

    def as_dict(self) -> dict[str, str]:
        return {
            "child": self.child,
            "column": self.column,
            "parent": self.parent,
            "parent_column": self.parent_column,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    order: tuple[str, ...]
    edges: tuple[DependencyEdge, ...]
    self_references: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "order": list(self.order),
            "edges": [edge.as_dict() for edge in self.edges],
            "self_references": [{"table": table, "column": column} for table, column in self.references],
        }

    @property
    def references(self) -> tuple[tuple[str, str], ...]:
        return self.self_references

    def readable(self) -> str:
        lines = ["Execution plan:"]
        lines.extend(f"  {index + 1}. {name}" for index, name in enumerate(self.order))
        if self.edges:
            lines.append("Dependencies:")
            lines.extend(
                f"  {edge.child}.{edge.column} -> {edge.parent}.{edge.parent_column}"
                for edge in self.edges
            )
        if self.self_references:
            lines.append("Self references (earlier rows only):")
            lines.extend(f"  {table}.{column} -> {table}" for table, column in self.self_references)
        return "\n".join(lines)


def plan_dataset(dataset: DatasetSpec) -> ExecutionPlan:
    names = dataset.table_names
    known = set(names)
    dependencies: dict[str, set[str]] = {name: set() for name in names}
    edges: list[DependencyEdge] = []
    self_refs: list[tuple[str, str]] = []
    for table in dataset.tables:
        for column in table.columns:
            if column.references is None:
                continue
            parent = column.references.table
            if parent not in known:
                raise SynthlabError(
                    "SPEC004",
                    f"reference target table {parent!r} does not exist",
                    table=table.name,
                    column=column.name,
                )
            edge = DependencyEdge(table.name, column.name, parent, column.references.column)
            edges.append(edge)
            if parent == table.name:
                self_refs.append((table.name, column.name))
            else:
                dependencies[table.name].add(parent)

    order: list[str] = []
    emitted: set[str] = set()
    remaining = set(names)
    while remaining:
        ready = sorted(name for name in remaining if dependencies[name] <= emitted)
        if not ready:
            blocked = ", ".join(sorted(remaining))
            raise SynthlabError(
                "SPEC005",
                f"cyclic table dependency blocks planning: {blocked}",
            )
        order.extend(ready)
        emitted.update(ready)
        remaining.difference_update(ready)

    return ExecutionPlan(
        order=tuple(order),
        edges=tuple(sorted(edges, key=lambda edge: (edge.child, edge.column, edge.parent, edge.parent_column))),
        self_references=tuple(sorted(self_refs)),
    )
