"""Row-level foreign key assignment constrained by a parent attribute.

A ``distribute:`` declaration on a foreign key column is turned into a
concrete, deterministic row -> parent key plan *before* any child row is
generated. Feasibility is decided from the parent's actual generated values
and the child row count (unique capacity, forced NULLs, boundary-forced
values and deterministic retries included); infeasible declarations raise
``GEN004`` before anything is exported. The class of every parent key is
always read from the parent's generated data, never inferred from text.
"""

from __future__ import annotations

from typing import Any

from .errors import GenerationError
from .generators.common import as_text, derive_seed, make_rng, normalize_enum, parse_date, parse_datetime
from .spec.model import ColumnSpec, DatasetSpec, DistributionSpec, TableSpec

_UNLISTED = object()  # slot marker for the pool of unlisted attribute values
_MISSING = object()

ROUTE_HASH = "hash"
ROUTE_CAPACITY = "capacity"


def resolve_counts(dist: DistributionSpec, n: int) -> tuple[tuple[tuple[Any, int], ...], int]:
    """Return ``(ordered (value, count) pairs, unlisted remainder)`` for ``n`` rows.

    Quotas are used verbatim; proportions are apportioned with the largest
    remainder method (declaration order breaks ties) so the result is exact
    and deterministic. The remainder is the number of rows left for parent
    attribute values not listed in the declaration.
    """
    if dist.mode == "quotas":
        counts = tuple((value, int(amount)) for value, amount in dist.entries)
        return counts, n - sum(count for _, count in counts)
    buckets: list[tuple[Any, float]] = [(value, float(share)) for value, share in dist.entries]
    if dist.unlisted == "allow":
        listed = sum(share for _, share in buckets)
        buckets.append((_UNLISTED, max(0.0, 1.0 - listed)))
    raw = [(value, share * n) for value, share in buckets]
    seats: list[int] = [int(part) for _, part in raw]
    leftover = n - sum(seats)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i][1] - seats[i]), i))
    cursor = 0
    while leftover > 0 and order:
        seats[order[cursor % len(order)]] += 1
        cursor += 1
        leftover -= 1
    counts = tuple(
        (value, seats[index]) for index, (value, _) in enumerate(raw) if value is not _UNLISTED
    )
    remainder = next((seats[index] for index, (value, _) in enumerate(raw) if value is _UNLISTED), 0)
    return counts, remainder


def _sibling_potential(column: ColumnSpec | None, rows: int,
                       column_pools: dict[str, dict[str, list[Any]]]) -> int:
    """Upper bound on distinct values a sibling column can contribute."""
    if column is None:
        return 1
    kind = column.type
    if kind == "bool":
        return min(rows, 2)
    if kind == "enum":
        return min(rows, max(1, len(normalize_enum(column.param("values", column.param("choices"))))))
    if kind == "int":
        low = int(column.param("min", 0))
        high = int(column.param("max", 1000))
        step = max(1, int(column.param("step", 1)))
        return min(rows, (high - low) // step + 1)
    if kind == "date":
        span = (parse_date(column.param("end", "2024-12-31"))
                - parse_date(column.param("start", "2020-01-01"))).days
        return min(rows, span // max(1, int(column.param("step_days", 1))) + 1)
    if kind == "datetime":
        start = parse_datetime(column.param("start", "2020-01-01T00:00:00Z"))
        end = parse_datetime(column.param("end", "2024-12-31T23:59:59Z"))
        span = int((end - start).total_seconds())
        return min(rows, span // max(1, int(column.param("step_seconds", 1))) + 1)
    if kind == "foreign_key" and column.references is not None:
        pool = column_pools.get(column.references.table, {}).get(column.references.column)
        if pool is not None:
            return min(rows, max(1, len({value for value in pool if value is not None})))
        return rows
    # sequence, uuid, string, text, float, decimal and anything else have
    # effectively unbounded distinct-value potential at this scale.
    return rows


def key_capacity(table: TableSpec, column_name: str,
                 column_pools: dict[str, dict[str, list[Any]]]) -> int:
    """How often one parent key may appear in ``column_name`` (unique groups)."""
    cap = table.rows
    for group in table.unique_groups:
        if column_name not in group:
            continue
        if len(group) == 1:
            return 1
        potential = table.rows
        for sibling in group:
            if sibling == column_name:
                continue
            potential = min(potential, _sibling_potential(table.column(sibling), table.rows, column_pools))
        cap = min(cap, potential)
    return max(cap, 1)


def plan_distributions(
    dataset: DatasetSpec,
    table: TableSpec,
    forced: dict[tuple[int, str], Any],
    column_pools: dict[str, dict[str, list[Any]]],
) -> tuple[dict[tuple[int, str], Any], dict[str, dict[str, Any]]]:
    """Plan forced foreign key values for every ``distribute:`` column.

    Returns ``(forced_updates, manifest_info)``. Raises ``GEN004`` with a
    stable, itemized message when the declaration cannot be satisfied.
    """
    updates: dict[tuple[int, str], Any] = {}
    infos: dict[str, dict[str, Any]] = {}
    for column in table.columns:
        dist = column.distribute
        if dist is None:
            continue
        ref = column.references
        parent_name = ref.table if ref else ""
        parent_column = ref.column if ref else ""
        parent_keys = list(column_pools.get(parent_name, {}).get(parent_column, []))
        parent_attrs = list(column_pools.get(parent_name, {}).get(dist.attribute, []))
        key_class: dict[Any, Any] = {}
        classes: dict[Any, list[Any]] = {}
        for key, attribute in zip(parent_keys, parent_attrs):
            if key is None:
                continue
            key_class[key] = attribute
            classes.setdefault(attribute, []).append(key)

        forced_nulls = {row for (row, col), value in forced.items()
                        if col == column.name and value is None}
        forced_values = {row: value for (row, col), value in forced.items()
                         if col == column.name and value is not None}
        assignable = table.rows - len(forced_nulls)
        counts, remainder = resolve_counts(dist, assignable)
        declared = {value for value, _ in counts}

        problems: list[str] = []
        if remainder < 0:
            problems.append(
                f"declared quotas sum to {assignable - remainder} but only "
                f"{assignable} row(s) are assignable"
            )
        if remainder > 0 and dist.unlisted == "forbid":
            problems.append(f"{remainder} row(s) have no listed attribute value while unlisted is forbid")

        forced_declared: dict[Any, int] = {}
        forced_unlisted = 0
        forced_usage: dict[Any, int] = {}
        for row, value in sorted(forced_values.items()):
            forced_usage[value] = forced_usage.get(value, 0) + 1
            cls = key_class.get(value, _MISSING)
            if cls is _MISSING:
                problems.append(f"forced value {value!r} on row {row} is not a generated parent key")
            elif cls in declared:
                forced_declared[cls] = forced_declared.get(cls, 0) + 1
            else:
                forced_unlisted += 1
        for value, count in counts:
            if count > 0 and not classes.get(value):
                problems.append(f"no generated parent keys for attribute value {value!r}")
            if forced_declared.get(value, 0) > count:
                problems.append(
                    f"boundary-forced values exceed the quota for attribute value {value!r}"
                )
        if dist.unlisted == "forbid" and forced_unlisted:
            problems.append(f"{forced_unlisted} forced value(s) belong to unlisted attribute values")
        if dist.unlisted == "allow" and forced_unlisted > max(remainder, 0):
            problems.append(f"forced values exceed the unlisted remainder of {max(remainder, 0)}")

        cap = key_capacity(table, column.name, column_pools)
        for value, count in counts:
            keys = classes.get(value, [])
            available = sum(max(cap - forced_usage.get(key, 0), 0) for key in keys)
            need = count - forced_declared.get(value, 0)
            if need > available:
                problems.append(
                    f"attribute value {value!r} needs {need} more row(s) but {len(keys)} parent "
                    f"key(s) provide {available} slot(s) (unique capacity {cap} per key)"
                )
        unlisted_keys = [key for attribute, keys in classes.items() if attribute not in declared
                         for key in keys]
        remainder_need = max(remainder - forced_unlisted, 0) if remainder > 0 else 0
        if remainder_need:
            available = sum(max(cap - forced_usage.get(key, 0), 0) for key in unlisted_keys)
            if remainder_need > available:
                problems.append(
                    f"unlisted remainder needs {remainder_need} row(s) but only {available} "
                    f"key slot(s) remain"
                )
        if problems:
            raise GenerationError(
                "GEN004",
                f"distribution on {table.name}.{column.name} is infeasible: " + "; ".join(problems),
                table=table.name,
                column=column.name,
            )

        slots: list[Any] = []
        for value, count in counts:
            slots.extend([value] * (count - forced_declared.get(value, 0)))
        slots.extend([_UNLISTED] * remainder_need)
        rng = make_rng(dataset.seed, dataset.name, table.name, column.name, "distribute")
        rng.shuffle(slots)
        free_rows = [row for row in range(table.rows)
                     if row not in forced_nulls and row not in forced_values]

        pools: dict[Any, list[Any]] = {value: list(classes.get(value, [])) for value in declared}
        pools[_UNLISTED] = unlisted_keys
        usage: dict[Any, int] = dict(forced_usage)
        if dist.route == ROUTE_CAPACITY:
            cursors: dict[Any, int] = {}
            for row, slot in zip(free_rows, slots):
                pool = pools[slot]
                position = cursors.get(slot, 0)
                while position < len(pool) and usage.get(pool[position], 0) >= cap:
                    position += 1
                if position >= len(pool):
                    raise GenerationError(
                        "GEN004",
                        f"distribution on {table.name}.{column.name} exhausted key capacity",
                        table=table.name, column=column.name,
                    )
                key = pool[position]
                usage[key] = usage.get(key, 0) + 1
                cursors[slot] = position
                updates[(row, column.name)] = key
        else:
            for row, slot in zip(free_rows, slots):
                pool = pools[slot]
                if not pool:
                    raise GenerationError(
                        "GEN004",
                        f"distribution on {table.name}.{column.name} has no keys for a slot",
                        table=table.name, column=column.name,
                    )
                start = derive_seed(dataset.seed, dataset.name, table.name, column.name,
                                    "assign", row) % len(pool)
                chosen = None
                for offset in range(len(pool)):
                    candidate = pool[(start + offset) % len(pool)]
                    if usage.get(candidate, 0) < cap:
                        chosen = candidate
                        break
                if chosen is None:
                    raise GenerationError(
                        "GEN004",
                        f"distribution on {table.name}.{column.name} exhausted key capacity",
                        table=table.name, column=column.name,
                    )
                usage[chosen] = usage.get(chosen, 0) + 1
                updates[(row, column.name)] = chosen

        infos[column.name] = {
            "table": table.name,
            "column": column.name,
            "parent": parent_name,
            "parent_column": parent_column,
            "by": dist.attribute,
            "mode": dist.mode,
            "route": dist.route,
            "unlisted": dist.unlisted,
            "declared": {as_text(value): amount for value, amount in dist.entries},
            "resolved": {as_text(value): count for value, count in counts},
            "remainder": max(remainder, 0),
            "assignable_rows": assignable,
        }
    return updates, infos
