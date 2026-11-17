"""YAML blueprint loading and conversion into the domain model."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import SynthlabError
from .model import CheckSpec, ColumnSpec, DatasetSpec, ReferenceSpec, TableSpec


@dataclass(slots=True)
class Blueprint:
    path: str
    sha256: str
    raw_bytes: bytes
    data: dict[str, Any]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that refuses silently-overwritten duplicate mapping keys."""


def _construct_mapping(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise SynthlabError("SPEC003", f"duplicate mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_blueprint(path: str | Path) -> Blueprint:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SynthlabError("IO001", f"cannot read blueprint: {exc}", path=str(source)) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SynthlabError("IO002", "blueprint is not valid UTF-8", path=str(source)) from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except SynthlabError as exc:
        raise SynthlabError(
            exc.code, exc.message, path=str(source), issues=[
                issue.__class__(issue.code, issue.message, str(source), issue.table, issue.column)
                for issue in exc.issues
            ]
        ) from exc
    except yaml.YAMLError as exc:
        raise SynthlabError("SPEC000", f"invalid YAML: {exc}", path=str(source)) from exc
    if not isinstance(data, dict):
        raise SynthlabError("SPEC000", "blueprint root must be a mapping", path=str(source))
    return Blueprint(str(source), hashlib.sha256(raw).hexdigest(), raw, data)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _parse_literal(text: str) -> Any:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        return stripped[1:-1]
    lowered = stripped.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped


def parse_check(value: Any) -> CheckSpec:
    if isinstance(value, dict):
        return CheckSpec(str(value["column"]), str(value["op"]), value.get("value"))
    if not isinstance(value, str):
        raise ValueError("check must be a string or mapping")
    for operator in (">=", "<=", "!=", "==", ">", "<"):
        if operator in value:
            left, right = value.split(operator, 1)
            return CheckSpec(left.strip(), operator, _parse_literal(right))
    raise ValueError("check must contain one of >=, <=, !=, ==, >, <")


def build_dataset(blueprint: Blueprint) -> DatasetSpec:
    raw = blueprint.data
    tables: list[TableSpec] = []
    for raw_table in raw.get("tables", []):
        columns: list[ColumnSpec] = []
        for raw_column in raw_table.get("columns", []):
            ref_value = raw_column.get("references")
            reference = None
            if isinstance(ref_value, str) and "." in ref_value:
                parent, parent_column = ref_value.rsplit(".", 1)
                reference = ReferenceSpec(parent, parent_column)
            known = {"name", "type", "nullable", "null_rate", "unique", "references", "boundary", "description"}
            params = {key: value for key, value in raw_column.items() if key not in known}
            columns.append(
                ColumnSpec(
                    name=str(raw_column.get("name")),
                    type=str(raw_column.get("type")),
                    nullable=bool(raw_column.get("nullable", True)),
                    null_rate=float(raw_column.get("null_rate", 0.0)),
                    unique=bool(raw_column.get("unique", False)),
                    params=params,
                    references=reference,
                    boundary=tuple("null" if item is None else str(item) for item in _as_list(raw_column.get("boundary"))),
                )
            )
        constraints = tuple(tuple(str(name) for name in group) for group in _as_list(raw_table.get("unique_constraints")))
        checks: list[CheckSpec] = []
        for raw_check in _as_list(raw_table.get("checks")):
            checks.append(parse_check(raw_check))
        tables.append(
            TableSpec(
                name=str(raw_table.get("name")),
                rows=int(raw_table.get("rows", 0)),
                primary_key=str(raw_table.get("primary_key")),
                columns=tuple(columns),
                unique_constraints=constraints,
                checks=tuple(checks),
            )
        )
    return DatasetSpec(
        name=str(raw.get("dataset")),
        seed=int(raw.get("seed", 0)),
        version=str(raw.get("version")),
        tables=tuple(tables),
        require_boundary_coverage=bool(raw.get("require_boundary_coverage", False)),
        max_rows=int(raw.get("max_rows", 100000)),
        source_path=blueprint.path,
        blueprint_sha256=blueprint.sha256,
        blueprint_bytes=blueprint.raw_bytes,
        raw=raw,
    )


def load_dataset(path: str | Path, *, validate: bool = True) -> DatasetSpec:
    blueprint = load_blueprint(path)
    if validate:
        from .validator import validate_blueprint

        validate_blueprint(blueprint)
    return build_dataset(blueprint)
