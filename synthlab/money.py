"""Exact money arithmetic and stable allocation primitives.

All monetary values inside the settlement pipeline are integer cents.
Decimal strings are converted at the parse boundary; binary float is
rejected outright so it can never enter an accounting decision.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

CENT = Decimal("0.01")


def to_cents(value: Any) -> int:
    """Convert a decimal string / int / Decimal to whole cents.

    ``float`` is rejected because binary floating point cannot represent
    decimal currency exactly.  Fractional cents are rejected instead of
    silently rounded, so a malformed fixture never produces quiet drift.
    """
    if isinstance(value, bool):
        raise TypeError("boolean is not a monetary amount")
    if isinstance(value, float):
        raise TypeError("binary float is forbidden; pass str, int or Decimal")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("negative amounts need an explicit signed field")
        return value
    decimal_value = Decimal(str(value))
    if decimal_value != decimal_value.quantize(CENT):
        raise ValueError(f"amount {value!s} is not whole cents")
    return int(decimal_value * 100)


def format_cents(value: int) -> str:
    """Render integer cents as a canonical two-decimal string."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def split_units(total_cents: int, units: int) -> list[int]:
    """Split ``total_cents`` across ``units`` stable unit indices.

    The first ``total % units`` indices receive the extra remainder cent,
    so every unit is a whole cent, the parts always sum to the total and
    the allocation never depends on return processing order.
    """
    if units <= 0:
        raise ValueError("units must be positive")
    if total_cents < 0:
        raise ValueError("cannot split a negative total")
    base, remainder = divmod(total_cents, units)
    return [base + (1 if index < remainder else 0) for index in range(units)]


def allocate_by_weight(weights: Sequence[int], amount: int) -> list[int]:
    """Largest-remainder allocation of ``amount`` across weighted shares.

    Ties on the fractional remainder are broken by the stable input index,
    making the result deterministic for every caller (promotion shares,
    instrument spreads, ...).  The returned parts always sum to ``amount``.
    """
    if amount < 0:
        raise ValueError("cannot allocate a negative amount")
    total_weight = sum(weights)
    if total_weight <= 0:
        if amount != 0:
            raise ValueError("cannot allocate a positive amount without weight")
        return [0 for _ in weights]
    shares = [amount * weight // total_weight for weight in weights]
    leftover = amount - sum(shares)
    if leftover:
        fractions = sorted(
            range(len(weights)),
            key=lambda index: (
                -((amount * weights[index]) % total_weight),
                index,
            ),
        )
        for index in fractions[:leftover]:
            shares[index] += 1
    assert sum(shares) == amount
    return shares
