"""Static validation for blueprints. All failures carry stable SPEC codes."""

from __future__ import annotations

import re
from typing import Any

from ..errors import Issue, IssueBag, SpecValidationError
from .loader import Blueprint, parse_check
from .model import SUPPORTED_TYPES
from .rules import BOUNDARY_MARKERS, CHECK_OPERATORS, is_int, is_number, validate_params

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DISCRETE_TYPES = {"enum", "string", "bool", "int", "sequence", "foreign_key"}
DISTRIBUTE_ROUTES = {"hash", "capacity"}
DISTRIBUTE_UNLISTED = {"forbid", "allow"}


def _validate_distribute(raw: Any, table: str, column: str, nullable: bool, null_rate: Any,
                         boundary: list[Any], refs: Any, bag: IssueBag) -> dict[str, Any] | None:
    """Structural checks for a ``distribute:`` declaration on one column."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        bag.add("SPEC011", "distribute must be a mapping", table=table, column=column)
        return None
    attribute = raw.get("by")
    if not isinstance(attribute, str) or not attribute:
        bag.add("SPEC011", "distribute.by must name a parent attribute column", table=table, column=column)
        attribute = ""
    elif not IDENTIFIER.fullmatch(attribute):
        bag.add("SPEC011", f"invalid distribute.by identifier {attribute!r}", table=table, column=column)
    quotas = raw.get("quotas")
    proportions = raw.get("proportions")
    if quotas is not None and proportions is not None:
        bag.add("SPEC011", "distribute accepts either quotas or proportions, not both",
                table=table, column=column)
    if quotas is None and proportions is None:
        bag.add("SPEC011", "distribute requires quotas or proportions", table=table, column=column)
    for label, entries, kind in (("quotas", quotas, "count"), ("proportions", proportions, "share")):
        if entries is None:
            continue
        if not isinstance(entries, dict) or not entries:
            bag.add("SPEC011", f"distribute.{label} must be a non-empty mapping", table=table, column=column)
            continue
        for value, amount in entries.items():
            if value is None or isinstance(value, (list, dict)):
                bag.add("SPEC011", f"distribute.{label} keys must be scalar attribute values",
                        table=table, column=column)
                break
            if kind == "count" and (not is_int(amount) or amount < 0):
                bag.add("SPEC011", f"distribute.quotas[{value!r}] must be a non-negative integer",
                        table=table, column=column)
            if kind == "share" and (not is_number(amount) or amount <= 0):
                bag.add("SPEC011", f"distribute.proportions[{value!r}] must be a positive number",
                        table=table, column=column)
    if isinstance(proportions, dict) and proportions and all(
        is_number(amount) for amount in proportions.values()
    ):
        total = sum(float(amount) for amount in proportions.values())
        if total > 1.0 + 1e-9:
            bag.add("SPEC011", "distribute.proportions must not sum above 1", table=table, column=column)
    unlisted = raw.get("unlisted", "forbid")
    if unlisted not in DISTRIBUTE_UNLISTED:
        bag.add("SPEC011", f"distribute.unlisted must be one of {sorted(DISTRIBUTE_UNLISTED)}",
                table=table, column=column)
        unlisted = "forbid"
    route = raw.get("route", "hash")
    if route not in DISTRIBUTE_ROUTES:
        bag.add("SPEC011", f"distribute.route must be one of {sorted(DISTRIBUTE_ROUTES)}",
                table=table, column=column)
        route = "hash"
    if is_number(null_rate) and null_rate > 0:
        bag.add("SPEC011", "distribute requires null_rate 0; use boundary [null] for forced NULLs",
                table=table, column=column)
    return {
        "by": attribute,
        "quotas": quotas if isinstance(quotas, dict) else None,
        "proportions": proportions if isinstance(proportions, dict) else None,
        "unlisted": unlisted,
        "route": route,
        "nullable": nullable,
        "boundary": list(boundary) if isinstance(boundary, list) else [],
        "references": refs,
    }


def _validate_dataset_header(raw: dict[str, Any], bag: IssueBag) -> None:
    if not isinstance(raw.get("dataset"), str) or not raw.get("dataset"):
        bag.add("SPEC001", "dataset name is required")
    if not is_int(raw.get("seed")):
        bag.add("SPEC001", "integer seed is required")
    if raw.get("version") is None or str(raw.get("version", "")).strip() == "":
        bag.add("SPEC001", "version is required")
    if "max_rows" in raw and (not is_int(raw["max_rows"]) or raw["max_rows"] <= 0):
        bag.add("SPEC006", "max_rows must be a positive integer")
    if "require_boundary_coverage" in raw and not isinstance(raw["require_boundary_coverage"], bool):
        bag.add("SPEC001", "require_boundary_coverage must be boolean")
    if not isinstance(raw.get("tables"), list) or not raw["tables"]:
        bag.add("SPEC001", "tables must be a non-empty list")


def _validate_column(raw: dict[str, Any], table: str, primary_key: str, bag: IssueBag) -> dict[str, Any] | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        bag.add("SPEC001", "column name is required", table=table)
        return None
    if not IDENTIFIER.fullmatch(name):
        bag.add("SPEC010", f"invalid column identifier {name!r}", table=table, column=name)
    column_type = raw.get("type")
    if column_type not in SUPPORTED_TYPES:
        bag.add("SPEC002", f"unknown column type {column_type!r}", table=table, column=name)
    nullable = raw.get("nullable", True)
    if not isinstance(nullable, bool):
        bag.add("SPEC001", "nullable must be boolean", table=table, column=name)
        nullable = True
    if name == primary_key:
        nullable = False
    null_rate = raw.get("null_rate", 0.0)
    if not is_number(null_rate) or not 0 <= null_rate <= 1:
        bag.add("SPEC006", "null_rate must be between 0 and 1", table=table, column=name)
    elif null_rate > 0 and not nullable:
        bag.add("SPEC006", "null_rate > 0 requires nullable: true", table=table, column=name)
    if "unique" in raw and not isinstance(raw["unique"], bool):
        bag.add("SPEC001", "unique must be boolean", table=table, column=name)
    boundary = raw.get("boundary", [])
    if not isinstance(boundary, list):
        bag.add("SPEC009", "boundary must be a list", table=table, column=name)
        boundary = []
    for marker in boundary:
        normalized_marker = "null" if marker is None else marker
        if normalized_marker not in BOUNDARY_MARKERS:
            bag.add("SPEC009", f"unknown boundary marker {marker!r}", table=table, column=name)
        elif normalized_marker == "null" and not nullable:
            bag.add("SPEC009", "null boundary requires a nullable column", table=table, column=name)
        elif normalized_marker == "empty" and column_type not in ("string", "text"):
            bag.add("SPEC009", "empty boundary is only valid for string or text", table=table, column=name)
    known = {"name", "type", "nullable", "null_rate", "unique", "references", "boundary", "description",
             "distribute"}
    params = {key: value for key, value in raw.items() if key not in known}
    if column_type in SUPPORTED_TYPES:
        validate_params(column_type, params, table, name, bag)
    refs = raw.get("references")
    if column_type == "foreign_key" and refs is None:
        bag.add("SPEC004", "foreign_key column requires references", table=table, column=name)
    if refs is not None and column_type != "foreign_key":
        bag.add("SPEC004", "references is only supported on foreign_key columns", table=table, column=name)
    if refs is not None and (not isinstance(refs, str) or refs.count(".") != 1):
        bag.add("SPEC004", "references must use 'table.column'", table=table, column=name)
    distribute = _validate_distribute(raw.get("distribute"), table, name, nullable, null_rate,
                                      boundary, refs, bag)
    if distribute is not None and column_type != "foreign_key":
        bag.add("SPEC011", "distribute is only supported on foreign_key columns", table=table, column=name)
    return {
        "name": name,
        "type": column_type,
        "nullable": nullable,
        "unique": bool(raw.get("unique", False)),
        "references": refs,
        "distribute": distribute,
    }


def _validate_table(raw: Any, max_rows: int, bag: IssueBag) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        bag.add("SPEC001", "each table must be a mapping")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        bag.add("SPEC001", "table name is required")
        return None
    if not IDENTIFIER.fullmatch(name):
        bag.add("SPEC010", f"invalid table identifier {name!r}", table=name)
    rows = raw.get("rows")
    if not is_int(rows):
        bag.add("SPEC001", "rows must be an integer", table=name)
    elif rows < 0 or rows > max_rows:
        bag.add("SPEC006", f"rows must be between 0 and {max_rows}", table=name)
    primary_key = raw.get("primary_key")
    if not isinstance(primary_key, str) or not primary_key:
        bag.add("SPEC001", "primary_key is required", table=name)
        primary_key = ""
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        bag.add("SPEC001", "columns must be a non-empty list", table=name)
        raw_columns = []
    columns: list[dict[str, Any]] = []
    seen_columns: set[str] = set()
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            bag.add("SPEC001", "each column must be a mapping", table=name)
            continue
        column = _validate_column(raw_column, name, primary_key, bag)
        if column is None:
            continue
        if column["name"] in seen_columns:
            bag.add("SPEC003", f"duplicate column {column['name']!r}", table=name, column=column["name"])
        seen_columns.add(column["name"])
        columns.append(column)
    if primary_key and primary_key not in seen_columns:
        bag.add("SPEC001", f"primary_key {primary_key!r} is not a column", table=name)
    constraint_keys: set[frozenset[str]] = set()
    raw_constraints = raw.get("unique_constraints", [])
    if raw_constraints is not None and not isinstance(raw_constraints, list):
        bag.add("SPEC008", "unique_constraints must be a list", table=name)
        raw_constraints = []
    for group in raw_constraints or []:
        if not isinstance(group, list) or not group or not all(isinstance(item, str) for item in group):
            bag.add("SPEC008", "each unique constraint must be a non-empty list of columns", table=name)
            continue
        if len(set(group)) != len(group):
            bag.add("SPEC008", "unique constraint contains duplicate columns", table=name)
        unknown = [item for item in group if item not in seen_columns]
        if unknown:
            bag.add("SPEC008", f"unique constraint references unknown columns: {unknown}", table=name)
        key = frozenset(group)
        if key in constraint_keys:
            bag.add("SPEC008", "duplicate unique constraint", table=name)
        constraint_keys.add(key)
    checks = raw.get("checks", [])
    if checks is not None and not isinstance(checks, list):
        bag.add("SPEC009", "checks must be a list", table=name)
        checks = []
    for raw_check in checks or []:
        try:
            check = parse_check(raw_check)
        except (KeyError, TypeError, ValueError) as exc:
            bag.add("SPEC009", f"invalid check: {exc}", table=name)
            continue
        if check.column not in seen_columns:
            bag.add("SPEC009", f"check references unknown column {check.column!r}", table=name, column=check.column)
        if check.operator not in CHECK_OPERATORS:
            bag.add("SPEC009", f"unsupported check operator {check.operator!r}", table=name, column=check.column)
    return {
        "name": name,
        "rows": rows if is_int(rows) else 0,
        "columns": columns,
        "primary_key": primary_key,
        "unique_constraints": raw_constraints or [],
    }


def _unique_names(table: dict[str, Any]) -> set[str]:
    names = {column["name"] for column in table["columns"] if column["unique"]}
    for group in table["unique_constraints"]:
        if isinstance(group, list):
            names.update(item for item in group if isinstance(item, str))
    if table["primary_key"]:
        names.add(table["primary_key"])
    return names


def _validate_references(tables: dict[str, dict[str, Any]], bag: IssueBag) -> None:
    for table in tables.values():
        for column in table["columns"]:
            refs = column["references"]
            if not isinstance(refs, str) or refs.count(".") != 1:
                continue
            parent_name, parent_column = refs.split(".", 1)
            parent = tables.get(parent_name)
            if parent is None:
                bag.add("SPEC004", f"reference target table {parent_name!r} does not exist",
                        table=table["name"], column=column["name"])
                continue
            parent_columns = {item["name"] for item in parent["columns"]}
            if parent_column not in parent_columns:
                bag.add("SPEC004", f"reference target column {refs!r} does not exist",
                        table=table["name"], column=column["name"])
                continue
            if parent_column not in _unique_names(parent):
                bag.add("SPEC004", f"reference target {refs!r} is not a primary key or unique column",
                        table=table["name"], column=column["name"])
            if parent_name == table["name"]:
                if not column["nullable"]:
                    bag.add("SPEC004", "self-referencing foreign keys must be nullable",
                            table=table["name"], column=column["name"])
                if parent_column != parent["primary_key"]:
                    bag.add("SPEC004", "self-reference must target the table primary key",
                            table=table["name"], column=column["name"])


def _validate_distribution_targets(tables: dict[str, dict[str, Any]], bag: IssueBag) -> None:
    """Cross-table checks for distribute declarations (attribute, arithmetic)."""
    for table in tables.values():
        for column in table["columns"]:
            dist = column.get("distribute")
            if not dist:
                continue
            refs = dist["references"]
            if not isinstance(refs, str) or refs.count(".") != 1:
                continue
            parent_name, _ = refs.split(".", 1)
            if parent_name == table["name"]:
                bag.add("SPEC011", "distribute is not supported on self-referencing foreign keys",
                        table=table["name"], column=column["name"])
                continue
            parent = tables.get(parent_name)
            if parent is None:
                continue  # SPEC004 already reported by _validate_references
            attribute = dist["by"]
            parent_columns = {item["name"]: item for item in parent["columns"]}
            if attribute and attribute not in parent_columns:
                bag.add("SPEC011",
                        f"distribute.by attribute {parent_name}.{attribute} does not exist",
                        table=table["name"], column=column["name"])
            elif attribute and parent_columns[attribute].get("type") not in DISCRETE_TYPES:
                bag.add("SPEC011",
                        f"distribute.by attribute {parent_name}.{attribute} must be a discrete "
                        f"column ({sorted(DISCRETE_TYPES)})",
                        table=table["name"], column=column["name"])
            rows = table["rows"]
            markers = {"null" if marker is None else marker for marker in dist["boundary"]}
            forced_nulls = 1 if rows > 0 and "null" in markers and dist["nullable"] else 0
            available = rows - forced_nulls
            quotas = dist["quotas"]
            if quotas and all(is_int(amount) and amount >= 0 for amount in quotas.values()):
                total = sum(quotas.values())
                if dist["unlisted"] == "forbid" and total != available:
                    bag.add("SPEC011",
                            f"distribute.quotas sum {total} must equal {available} "
                            f"assignable row(s) when unlisted is forbid",
                            table=table["name"], column=column["name"])
                if dist["unlisted"] == "allow" and total > available:
                    bag.add("SPEC011",
                            f"distribute.quotas sum {total} exceeds {available} assignable row(s)",
                            table=table["name"], column=column["name"])
            proportions = dist["proportions"]
            if proportions and dist["unlisted"] == "forbid" and all(
                is_number(amount) for amount in proportions.values()
            ):
                total = sum(float(amount) for amount in proportions.values())
                if abs(total - 1.0) > 1e-9:
                    bag.add("SPEC011",
                            "distribute.proportions must sum to 1 when unlisted is forbid",
                            table=table["name"], column=column["name"])


def _detect_cycle(tables: dict[str, dict[str, Any]]) -> list[str]:
    graph: dict[str, set[str]] = {name: set() for name in tables}
    for table in tables.values():
        for column in table["columns"]:
            refs = column["references"]
            if isinstance(refs, str) and refs.count(".") == 1:
                parent = refs.split(".", 1)[0]
                if parent != table["name"] and parent in graph:
                    graph[table["name"]].add(parent)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for parent in sorted(graph[node]):
            if state.get(parent, 0) == 1:
                return stack[stack.index(parent):] + [parent]
            if state.get(parent, 0) == 0:
                found = visit(parent)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for name in sorted(graph):
        if state.get(name, 0) == 0:
            found = visit(name)
            if found:
                return found
    return []


def collect_issues(blueprint: Blueprint) -> list[Issue]:
    bag = IssueBag(path=blueprint.path)
    raw = blueprint.data
    _validate_dataset_header(raw, bag)
    max_rows = raw.get("max_rows", 100000) if is_int(raw.get("max_rows")) else 100000
    tables: dict[str, dict[str, Any]] = {}
    for raw_table in raw.get("tables", []) if isinstance(raw.get("tables"), list) else []:
        table = _validate_table(raw_table, max_rows, bag)
        if table is None:
            continue
        if table["name"] in tables:
            bag.add("SPEC003", f"duplicate table {table['name']!r}", table=table["name"])
        tables[table["name"]] = table
    _validate_references(tables, bag)
    _validate_distribution_targets(tables, bag)
    cycle = _detect_cycle(tables)
    if cycle:
        bag.add("SPEC005", f"cyclic table dependency: {' -> '.join(cycle)}")
    return bag.issues


def validate_blueprint(blueprint: Blueprint) -> None:
    issues = collect_issues(blueprint)
    if issues:
        raise SpecValidationError(issues)
