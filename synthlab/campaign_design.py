"""Structural generator for a completely synthetic household campaign trial."""
from __future__ import annotations

def potential_exposure(customer_number: int, assigned: int) -> int:
    return int(customer_number % 11 == 0 or (assigned and customer_number % 5 != 0))

def potential_conversion(customer_number: int, exposed: int) -> int:
    return int(customer_number % 13 < 5 or (exposed and customer_number % 7 < 4))
