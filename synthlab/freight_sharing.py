"""Minimum-cost shared freight routing and per-merchant invoice settlement.

The optimizer works on a small, bounded instance (at most :data:`MAX_MERCHANTS`
merchants) and uses exact enumeration only:

* every legal trip is a depot->stops->depot permutation whose loaded units never
  exceed ``vehicle_capacity``; directed network edges are accumulated per leg;
* the cheapest fleet covering any merchant subset is found with set-partition
  dynamic programming, so each sub-coalition gets its own freshly optimized
  routes (a grand-coalition route is never reused for a sub-coalition);
* two independently authorized contracts turn the grand-coalition cost into
  per-merchant invoices: a units-proportional split and a Shapley marginal split,
  both rounded to cents with a stable largest-remainder rule and checked against
  each merchant's solo round-trip upper bound.

All distances, capacities and unit counts are exact integers, so every cost and
every cent allocation can be verified arithmetically.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, permutations
from typing import Sequence

from .errors import SynthlabError

MAX_MERCHANTS = 6


class FreightValidationError(SynthlabError):
    """The intake document cannot describe a legal freight batch."""


class FreightInfeasibleError(SynthlabError):
    """No legal fleet can cover every shipment under the capacity/network."""


class ContractRejectedError(SynthlabError):
    """A contract rule cannot be reconciled with the solo upper bounds."""


@dataclass(frozen=True, slots=True)
class Shipment:
    merchant_id: str
    stop: str
    sku_id: str
    units: int


@dataclass(frozen=True, slots=True)
class Trip:
    """One depot-to-depot vehicle trip with full audit evidence."""

    merchants: tuple[str, ...]
    stops: tuple[str, ...]
    units_loaded: int
    vehicle_capacity: int
    distance: int
    legs: tuple[tuple[str, str, int], ...]

    def as_dict(self) -> dict:
        return {
            "merchants": list(self.merchants),
            "stops": list(self.stops),
            "units_loaded": self.units_loaded,
            "vehicle_capacity": self.vehicle_capacity,
            "distance": self.distance,
            "legs": [
                {"from": u, "to": v, "distance": w} for u, v, w in self.legs
            ],
        }


# --------------------------------------------------------------------------- #
# Intake validation
# --------------------------------------------------------------------------- #

def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_intake(
    data: dict, valid_skus: set[str] | frozenset[str] | None = None
) -> tuple[str, int, list[Shipment]]:
    """Validate the intake document and return depot, capacity and shipments.

    Unknown SKUs are only checked when ``valid_skus`` is supplied; this keeps
    the freight module free of network/dataset dependencies while letting the
    CLI (or a catalog embedded in the fixture) pin the merchandise version.
    """

    def fail(code: str, message: str) -> None:
        raise FreightValidationError(code, message)

    if not isinstance(data, dict):
        fail("FV000", "freight intake must be a JSON object")

    depot = data.get("depot")
    if not isinstance(depot, str) or not depot:
        fail("FV001", "depot must be a non-empty string")

    capacity = data.get("vehicle_capacity")
    if not _is_real_int(capacity) or capacity <= 0:
        fail("FV002", "vehicle_capacity must be a positive integer")

    raw_merchants = data.get("merchants")
    if not isinstance(raw_merchants, list) or not raw_merchants:
        fail("FV003", "merchants must be a non-empty list of shipments")
    if len(raw_merchants) > MAX_MERCHANTS:
        fail(
            "FV004",
            f"at most {MAX_MERCHANTS} merchants are supported for exact search, "
            f"got {len(raw_merchants)}",
        )

    shipments: list[Shipment] = []
    seen_ids: set[str] = set()
    seen_stops: list[str] = []
    for index, raw in enumerate(raw_merchants):
        if not isinstance(raw, dict):
            fail("FV005", f"merchants[{index}] must be an object")
        merchant_id = raw.get("merchant_id")
        stop = raw.get("stop")
        sku_id = raw.get("sku_id")
        units = raw.get("units")
        if not isinstance(merchant_id, str) or not merchant_id:
            fail("FV006", f"merchants[{index}].merchant_id must be a non-empty string")
        if merchant_id in seen_ids:
            fail("FV007", f"duplicate merchant_id {merchant_id!r}")
        if not isinstance(stop, str) or not stop:
            fail("FV008", f"merchant {merchant_id!r}: stop must be a non-empty string")
        if not isinstance(sku_id, str) or not sku_id:
            fail("FV009", f"merchant {merchant_id!r}: sku_id must be a non-empty string")
        if not _is_real_int(units) or units <= 0:
            fail("FV010", f"merchant {merchant_id!r}: units must be a positive integer")
        if valid_skus is not None and sku_id not in valid_skus:
            fail("FV011", f"merchant {merchant_id!r}: unknown sku_id {sku_id!r}")
        seen_ids.add(merchant_id)
        if stop not in seen_stops:
            seen_stops.append(stop)
        shipments.append(Shipment(merchant_id, stop, sku_id, units))

    distance = data.get("distance")
    if not isinstance(distance, dict):
        fail("FV012", "distance must be an adjacency object")

    nodes = [depot, *seen_stops]
    for node in nodes:
        row = distance.get(node)
        if not isinstance(row, dict):
            fail("FV013", f"distance matrix is missing row for node {node!r}")
    for u in nodes:
        for v in nodes:
            if u == v:
                continue
            row = distance[u]
            if v not in row:
                fail("FV014", f"missing directed distance edge {u!r} -> {v!r}")
            weight = row[v]
            if not _is_real_int(weight) or weight < 0:
                fail("FV015", f"distance {u!r} -> {v!r} must be a non-negative integer")
    # Reject malformed weights even on rows/columns outside the active network.
    for u, row in distance.items():
        if not isinstance(row, dict):
            fail("FV016", f"distance row {u!r} must be an object")
        for v, weight in row.items():
            if not _is_real_int(weight) or weight < 0:
                fail("FV017", f"distance {u!r} -> {v!r} must be a non-negative integer")

    contracts = data.get("contracts")
    if contracts is not None:
        if not isinstance(contracts, dict):
            fail("FV018", "contracts must be an object of contract name to rule")
        for name, contract in contracts.items():
            if not isinstance(contract, dict):
                fail("FV019", f"contract {name!r} must be an object")
            rule = contract.get("charge_rule")
            if rule not in _CONTRACT_RULES:
                fail(
                    "FV020",
                    f"contract {name!r}: unsupported charge_rule {rule!r}; "
                    f"expected one of {sorted(_CONTRACT_RULES)}",
                )
    return depot, capacity, shipments


# --------------------------------------------------------------------------- #
# Exact route and fleet optimization
# --------------------------------------------------------------------------- #

def _edge(distance: dict, origin: str, dest: str) -> int:
    """Distance of a directed leg; staying at the same node costs zero."""
    return 0 if origin == dest else distance[origin][dest]


def _enumerate_trip_options(
    shipments: Sequence[Shipment],
    depot: str,
    capacity: int,
    distance: dict,
) -> dict[int, tuple[int, tuple[int, ...]]]:
    """Cheapest legal trip for every merchant subset that fits one vehicle.

    Returns ``subset_mask -> (cost, visit permutation)``. Permutations are
    visited in lexicographic order and only strict improvements replace the
    incumbent, so ties resolve deterministically by input order.
    """

    options: dict[int, tuple[int, tuple[int, ...]]] = {}
    n = len(shipments)
    for size in range(1, n + 1):
        for group in combinations(range(n), size):
            loaded = sum(shipments[idx].units for idx in group)
            if loaded > capacity:
                continue
            mask = 0
            for idx in group:
                mask |= 1 << idx
            best_cost: int | None = None
            best_perm: tuple[int, ...] | None = None
            for perm in permutations(group):
                cost = 0
                previous = depot
                for idx in perm:
                    stop = shipments[idx].stop
                    cost += _edge(distance, previous, stop)
                    previous = stop
                cost += _edge(distance, previous, depot)
                if best_cost is None or cost < best_cost:
                    best_cost, best_perm = cost, perm
            assert best_cost is not None and best_perm is not None
            options[mask] = (best_cost, best_perm)
    return options


def _build_trip(
    perm: Sequence[int],
    shipments: Sequence[Shipment],
    depot: str,
    capacity: int,
    distance: dict,
) -> Trip:
    stops: list[str] = [depot]
    legs: list[tuple[str, str, int]] = []
    loaded = 0
    previous = depot
    merchants: list[str] = []
    for idx in perm:
        shipment = shipments[idx]
        loaded += shipment.units
        if loaded > capacity:  # defensive: option enumeration already enforces this
            raise FreightInfeasibleError(
                "FI002",
                f"trip would carry {loaded} units but capacity is {capacity}",
            )
        if shipment.stop != previous:
            legs.append((previous, shipment.stop, _edge(distance, previous, shipment.stop)))
        stops.append(shipment.stop)
        merchants.append(shipment.merchant_id)
        previous = shipment.stop
    if previous != depot:
        legs.append((previous, depot, _edge(distance, previous, depot)))
    stops.append(depot)
    return Trip(
        merchants=tuple(merchants),
        stops=tuple(stops),
        units_loaded=loaded,
        vehicle_capacity=capacity,
        distance=sum(leg[2] for leg in legs),
        legs=tuple(legs),
    )


def solve_fleet(
    shipments: Sequence[Shipment], depot: str, capacity: int, distance: dict
) -> tuple[list[int | None], list[tuple[int, int] | None], dict[int, tuple[int, tuple[int, ...]]]]:
    """Set-partition DP giving the cheapest legal fleet cost for every mask.

    ``dp[mask]`` is ``None`` when no legal fleet covers that mask. Each trip's
    mileage is counted independently: two trips traversing the same leg pay it
    twice, exactly as driven.
    """

    options = _enumerate_trip_options(shipments, depot, capacity, distance)
    total_masks = 1 << len(shipments)
    dp: list[int | None] = [None] * total_masks
    chosen: list[tuple[int, int] | None] = [None] * total_masks
    dp[0] = 0
    for mask in range(1, total_masks):
        sub = mask
        while sub:
            option = options.get(sub)
            remainder = mask ^ sub
            if option is not None and dp[remainder] is not None:
                candidate = option[0] + dp[remainder]  # type: ignore[operator]
                if dp[mask] is None or candidate < dp[mask]:
                    dp[mask] = candidate
                    chosen[mask] = (sub, remainder)
            sub = (sub - 1) & mask
    return dp, chosen, options


def _plan_for_mask(
    mask: int,
    chosen: list[tuple[int, int] | None],
    options: dict[int, tuple[int, tuple[int, ...]]],
    shipments: Sequence[Shipment],
    depot: str,
    capacity: int,
    distance: dict,
) -> list[Trip]:
    trips: list[Trip] = []
    current = mask
    while current:
        decision = chosen[current]
        if decision is None:
            raise FreightInfeasibleError(
                "FI001",
                "no legal fleet can cover the requested shipments under vehicle_capacity",
            )
        trip_mask, previous = decision
        trips.append(
            _build_trip(options[trip_mask][1], shipments, depot, capacity, distance)
        )
        current = previous
    trips.sort(key=lambda trip: trip.merchants)
    return trips


def solo_quotes(data: dict) -> dict[str, int]:
    """Standalone depot->stop->depot round-trip price per merchant."""
    depot = data["depot"]
    return {
        m["merchant_id"]: data["distance"][depot][m["stop"]]
        + data["distance"][m["stop"]][depot]
        for m in data["merchants"]
    }


# --------------------------------------------------------------------------- #
# Contract math: exact shares, stable cent allocation, solo-bound enforcement
# --------------------------------------------------------------------------- #

def weight_shares(shipments: Sequence[Shipment], grand_cost: int) -> list[Fraction]:
    total_units = sum(shipment.units for shipment in shipments)
    return [
        Fraction(shipment.units * grand_cost, total_units) for shipment in shipments
    ]


def marginal_shares(
    shipments: Sequence[Shipment], costs: Sequence[int]
) -> list[Fraction]:
    """Shapley value of each merchant under the exact fleet cost game.

    Every term re-optimizes both ``S`` and ``S u {i}`` from their sub-coalition
    costs; a single grand-coalition route is never reused.
    """

    n = len(shipments)
    factorial_n = math.factorial(n)
    shares: list[Fraction] = []
    for merchant in range(n):
        value = Fraction(0)
        others = [idx for idx in range(n) if idx != merchant]
        for size in range(n):
            subset_weight = Fraction(
                math.factorial(size) * math.factorial(n - size - 1), factorial_n
            )
            for group in combinations(others, size):
                subset_mask = 0
                for idx in group:
                    subset_mask |= 1 << idx
                marginal = costs[subset_mask | (1 << merchant)] - costs[subset_mask]
                value += subset_weight * marginal  # type: ignore[operator]
        shares.append(value)
    efficiency = sum(shares, Fraction(0))
    if efficiency != costs[-1]:
        raise ContractRejectedError(
            "FC003",
            f"marginal shares sum to {efficiency} but grand coalition costs "
            f"{costs[-1]}",
        )
    return shares


def allocate_cents(
    exact: Sequence[Fraction], merchant_ids: Sequence[str], total_cents: int
) -> list[int]:
    """Largest-remainder allocation of ``total_cents`` with stable id tie-break."""

    scaled = [share * 100 for share in exact]
    floors = [value.numerator // value.denominator for value in scaled]
    remainder_cents = total_cents - sum(floors)
    if not 0 <= remainder_cents <= len(exact):
        raise ContractRejectedError(
            "FC002",
            "exact shares do not round to the invoice total without losing cents",
        )
    order = sorted(
        range(len(exact)),
        key=lambda idx: (-(scaled[idx] - floors[idx]), merchant_ids[idx]),
    )
    cents = floors[:]
    for idx in order[:remainder_cents]:
        cents[idx] += 1
    if sum(cents) != total_cents:  # defensive conservation guard
        raise ContractRejectedError("FC002", "cent allocation is not conserved")
    return cents


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _issue_contract(
    name: str,
    rule: str,
    exact: Sequence[Fraction],
    shipments: Sequence[Shipment],
    grand_cost: int,
    solo: dict[str, int],
    marginal_evidence: dict[str, list[dict]] | None = None,
) -> dict:
    merchant_ids = [shipment.merchant_id for shipment in shipments]
    total_cents = grand_cost * 100
    cents = allocate_cents(exact, merchant_ids, total_cents)
    reasons: list[str] = []
    invoices: list[dict] = []
    for shipment, share, invoice_cents in zip(shipments, exact, cents):
        solo_cents = solo[shipment.merchant_id] * 100
        within_bound = invoice_cents <= solo_cents
        non_negative = invoice_cents >= 0
        if not non_negative:
            reasons.append(
                f"{name}: invoice for {shipment.merchant_id} is negative "
                f"({_money(invoice_cents)})"
            )
        if not within_bound:
            reasons.append(
                f"{name}: invoice {_money(invoice_cents)} for "
                f"{shipment.merchant_id} exceeds solo upper bound "
                f"{_money(solo_cents)}"
            )
        invoice = {
            "merchant_id": shipment.merchant_id,
            "exact_share": str(share),
            "amount_cents": invoice_cents,
            "amount": _money(invoice_cents),
            "solo_quote": solo[shipment.merchant_id],
            "solo_upper_bound_cents": solo_cents,
            "non_negative": non_negative,
            "within_solo_bound": within_bound,
        }
        invoices.append(invoice)
    if sum(cents) != total_cents:
        reasons.append(
            f"{name}: invoices total {sum(cents)} cents but common cost is "
            f"{total_cents} cents"
        )
    payload: dict = {
        "charge_rule": rule,
        "total_cents": total_cents,
        "total_amount": _money(total_cents),
        "conservation_ok": sum(cents) == total_cents,
    }
    if reasons:
        payload["status"] = "rejected"
        payload["reasons"] = reasons
        return payload
    payload["status"] = "issued"
    payload["invoices"] = invoices
    if marginal_evidence is not None:
        payload["marginal_terms"] = marginal_evidence
    return payload


_CONTRACT_RULES = {
    "units_proportional": "weight_share",
    "coalition_shapley": "marginal_share",
}


# --------------------------------------------------------------------------- #
# End-to-end settlement
# --------------------------------------------------------------------------- #

def _fingerprint(data: dict) -> str:
    canonical = json.dumps(
        {
            "depot": data["depot"],
            "vehicle_capacity": data["vehicle_capacity"],
            "merchants": data["merchants"],
            "distance": data["distance"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def settle(data: dict, valid_skus: set[str] | frozenset[str] | None = None) -> dict:
    """Validate, optimize every coalition and issue both contract invoices."""

    if isinstance(data.get("catalog"), list) and valid_skus is None:
        valid_skus = frozenset(str(sku) for sku in data["catalog"])
    depot, capacity, shipments = parse_intake(data, valid_skus)
    distance = data["distance"]
    n = len(shipments)
    full_mask = (1 << n) - 1

    dp, chosen, options = solve_fleet(shipments, depot, capacity, distance)
    if dp[full_mask] is None:
        overflow = [
            shipment.merchant_id
            for shipment in shipments
            if shipment.units > capacity
        ]
        detail = (
            f"shipments {overflow} each exceed vehicle_capacity={capacity}"
            if overflow
            else "capacity and directed network cannot cover every shipment"
        )
        raise FreightInfeasibleError(
            "FI001", f"no feasible shared-freight routing exists: {detail}"
        )

    merchant_ids = [shipment.merchant_id for shipment in shipments]
    solo = solo_quotes(data)
    grand_cost = dp[full_mask]
    assert isinstance(grand_cost, int)

    coalition_costs: list[dict] = []
    for mask in range(1 << n):
        members = [merchant_ids[idx] for idx in range(n) if mask & (1 << idx)]
        trips = _plan_for_mask(
            mask, chosen, options, shipments, depot, capacity, distance
        )
        coalition_costs.append(
            {
                "coalition": members,
                "size": len(members),
                "cost": dp[mask],
                "trips": [trip.as_dict() for trip in trips],
            }
        )
    coalition_costs.sort(key=lambda entry: (entry["size"], entry["coalition"]))

    grand_trips = _plan_for_mask(
        full_mask, chosen, options, shipments, depot, capacity, distance
    )

    # Marginal evidence: every sub-coalition term used in the Shapley average.
    marginal_evidence: dict[str, list[dict]] = {}
    factorial_n = math.factorial(n)
    for merchant in range(n):
        others = [idx for idx in range(n) if idx != merchant]
        terms: list[dict] = []
        for size in range(n):
            weight = Fraction(
                math.factorial(size) * math.factorial(n - size - 1), factorial_n
            )
            for group in combinations(others, size):
                subset_mask = 0
                for idx in group:
                    subset_mask |= 1 << idx
                marginal = dp[subset_mask | (1 << merchant)] - dp[subset_mask]
                terms.append(
                    {
                        "coalition_before": [
                            merchant_ids[idx] for idx in group
                        ],
                        "permutation_weight": str(weight),
                        "cost_without": dp[subset_mask],
                        "cost_with": dp[subset_mask | (1 << merchant)],
                        "marginal_cost": marginal,
                    }
                )
        marginal_evidence[merchant_ids[merchant]] = terms

    exact_weight = weight_shares(shipments, grand_cost)
    exact_marginal = marginal_shares(shipments, dp)  # type: ignore[arg-type]

    configured = data.get("contracts") or {
        "weight_share": {"charge_rule": "units_proportional"},
        "marginal_share": {"charge_rule": "coalition_shapley"},
    }
    contracts: dict[str, dict] = {}
    exact_by_rule = {
        "units_proportional": exact_weight,
        "coalition_shapley": exact_marginal,
    }
    for contract_name, contract in configured.items():
        rule = contract["charge_rule"]
        evidence = marginal_evidence if rule == "coalition_shapley" else None
        contracts[contract_name] = _issue_contract(
            contract_name,
            rule,
            exact_by_rule[rule],
            shipments,
            grand_cost,
            solo,
            marginal_evidence=evidence,
        )

    return {
        "input_fingerprint": _fingerprint(data),
        "depot": depot,
        "vehicle_capacity": capacity,
        "merchants": [
            {
                "merchant_id": shipment.merchant_id,
                "stop": shipment.stop,
                "sku_id": shipment.sku_id,
                "units": shipment.units,
                "solo_quote": solo[shipment.merchant_id],
            }
            for shipment in shipments
        ],
        "solo_quotes": dict(sorted(solo.items())),
        "solo_total": sum(solo.values()),
        "grand_coalition": {
            "coalition": sorted(merchant_ids),
            "cost": grand_cost,
            "trips": [trip.as_dict() for trip in grand_trips],
        },
        "coalition_costs": coalition_costs,
        "contracts": contracts,
    }
