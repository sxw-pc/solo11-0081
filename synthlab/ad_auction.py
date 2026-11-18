"""Combinatorial ad-slot auction with certificated allocation and settlement.

One auction round sells the ad slots of a single product SKU drawn from the
engine-generated demo catalog. Every bid may request one or more slots; all
bids submitted by the same advertiser are mutually exclusive alternatives of
which at most one may win. The allocation maximizes the total winning value
by exact bounded search (at most 8 bids and 4 slots), breaking ties towards
fewer winning advertisers and then towards the lexicographically smallest
sorted tuple of winning bid ids.

Two independent settlement contracts are supported, both anchored to the
*same* optimal allocation -- the payment rule never relaxes the allocation
integrity rules:

``first_price``
    Every winning bid pays its own value.
``externality_price``
    Every winning advertiser pays the externality its presence imposes on the
    other advertisers: the optimal total value recomputed after removing
    *all* of that advertiser's bids (winning or not), minus the value the
    other winners contribute in the original allocation. A negative price
    invalidates the certificate; it is never silently clamped to zero.

The certificate emitted by :func:`build_certificate` records the verified
SKU/catalog identity, the per-slot winners, the mutual-exclusion basis, the
tie-break rules, the counterfactual allocation behind every externality
invoice and the reason every losing bid was rejected, so the whole round can
be recomputed offline. No external auction service is involved.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .engine import generate_dataset
from .errors import Issue, SynthlabError
from .spec.model import DatasetSpec

MAX_BIDS = 8
MAX_SLOTS = 4

FIRST_PRICE = "first_price"
EXTERNALITY_PRICE = "externality_price"
PAYMENT_RULES = {FIRST_PRICE: "winning_bid", EXTERNALITY_PRICE: "vcg_externality"}

TIE_BREAK_RULES = (
    "maximize the total value of winning bids",
    "then minimize the number of distinct winning advertisers",
    "then choose the lexicographically smallest sorted tuple of winning bid_id values",
)


class AuctionError(SynthlabError):
    """Intake validation or settlement integrity failure."""


class IntakeValidationError(AuctionError):
    """Aggregated intake validation failure listing every problem found."""

    def __init__(self, issues: Iterable[Issue]) -> None:
        collected = list(issues)
        if not collected:
            collected = [Issue("AUC001", "intake validation failed")]
        first = collected[0]
        summary = (
            first.message
            if len(collected) == 1
            else f"{len(collected)} intake problems; first: {first.message}"
        )
        super().__init__(first.code, summary, issues=collected)


@dataclass(frozen=True, slots=True)
class Bid:
    """One validated bid; alternative bids share their ``advertiser``."""

    bid_id: str
    advertiser: str
    slots: tuple[str, ...]
    value: int | float


@dataclass(frozen=True, slots=True)
class Intake:
    """Validated auction round, canonicalized (bids sorted by bid_id)."""

    sku_id: str
    slots: tuple[str, ...]
    bids: tuple[Bid, ...]
    contracts: tuple[str, ...]
    max_bids: int
    max_slots: int


@dataclass(frozen=True, slots=True)
class Allocation:
    """A feasible set of winning bids plus audit metadata."""

    winners: tuple[Bid, ...]
    total_value: int | float
    feasible_count: int = 0

    @property
    def winner_ids(self) -> tuple[str, ...]:
        return tuple(bid.bid_id for bid in self.winners)

    @property
    def advertiser_count(self) -> int:
        return len({bid.advertiser for bid in self.winners})


@dataclass(frozen=True, slots=True)
class CatalogView:
    """The engine-generated product catalog an auction round is bound to."""

    dataset: str
    blueprint_sha256: str
    products: tuple[tuple[Any, ...], ...]

    @property
    def skus(self) -> frozenset[str]:
        return frozenset(str(row[0]) for row in self.products)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps([list(row) for row in self.products], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def catalog_from_spec(spec: DatasetSpec) -> CatalogView:
    """Generate the demo dataset and expose its products table as a catalog."""
    data = generate_dataset(spec)
    table = data.tables.get("products")
    if table is None:
        raise AuctionError(
            "AUC011", f"dataset {spec.name!r} has no products table; auction catalog unavailable"
        )
    return CatalogView(
        dataset=spec.name,
        blueprint_sha256=spec.blueprint_sha256,
        products=tuple(tuple(row) for row in table.rows),
    )


# ---------------------------------------------------------------------------
# Intake validation
# ---------------------------------------------------------------------------


def _limit(value: Any, name: str, issues: list[Issue]) -> int:
    hard_cap = MAX_BIDS if name == "max_bids" else MAX_SLOTS
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        issues.append(Issue("AUC007", f"{name} must be a positive integer, got {value!r}"))
        return hard_cap
    if value > hard_cap:
        issues.append(Issue("AUC007", f"{name} {value} exceeds the supported bound {hard_cap}"))
        return hard_cap
    return value


def _validate_bid(
    item: Any, index: int, slot_set: frozenset[str], seen_ids: set[str], issues: list[Issue]
) -> Bid | None:
    where = f"bids[{index}]"
    if not isinstance(item, dict):
        issues.append(Issue("AUC001", f"{where} must be an object"))
        return None
    ok = True
    bid_id = item.get("bid_id")
    if not isinstance(bid_id, str) or not bid_id:
        issues.append(Issue("AUC008", f"{where}.bid_id must be a non-empty string"))
        ok = False
    elif bid_id in seen_ids:
        issues.append(Issue("AUC004", f"duplicate bid_id {bid_id!r}"))
        ok = False
    advertiser = item.get("advertiser")
    if not isinstance(advertiser, str) or not advertiser:
        issues.append(Issue("AUC008", f"{where}.advertiser must be a non-empty string"))
        ok = False
    slots_raw = item.get("slots")
    slots: list[str] = []
    if not isinstance(slots_raw, list) or not slots_raw:
        issues.append(Issue("AUC008", f"{where}.slots must request one or more slots"))
        ok = False
    else:
        for slot in slots_raw:
            if not isinstance(slot, str) or not slot:
                issues.append(Issue("AUC008", f"{where} slot names must be non-empty strings"))
                ok = False
            elif slot in slots:
                issues.append(Issue("AUC005", f"{where} requests slot {slot!r} twice"))
                ok = False
            elif slot not in slot_set:
                issues.append(Issue("AUC003", f"{where} requests unknown slot {slot!r}"))
                ok = False
            else:
                slots.append(slot)
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(Issue("AUC006", f"{where}.value must be a number, got {value!r}"))
        ok = False
    elif not math.isfinite(value) or value < 0:
        issues.append(Issue("AUC006", f"{where}.value must be non-negative and finite, got {value!r}"))
        ok = False
    if not ok:
        return None
    seen_ids.add(bid_id)
    return Bid(bid_id=bid_id, advertiser=advertiser, slots=tuple(slots), value=value)


def _validate_contracts(raw: Any, issues: list[Issue]) -> list[str]:
    if not isinstance(raw, dict) or not raw:
        issues.append(Issue("AUC009", "contracts must authorize at least one settlement contract"))
        return []
    names: list[str] = []
    for name, spec in raw.items():
        expected = PAYMENT_RULES.get(name)
        if expected is None:
            issues.append(Issue("AUC009", f"unknown settlement contract {name!r}"))
            continue
        rule = spec.get("payment_rule") if isinstance(spec, dict) else None
        if rule != expected:
            issues.append(
                Issue("AUC009", f"contract {name!r} must declare payment_rule {expected!r}, got {rule!r}")
            )
            continue
        names.append(name)
    return names


def validate_intake(data: Any, *, catalog_skus: frozenset[str]) -> Intake:
    """Validate raw intake JSON against the auction rules and the catalog.

    Raises :class:`IntakeValidationError` listing every problem found; nothing
    is allocated or settled from an invalid intake.
    """
    if not isinstance(data, dict):
        raise AuctionError("AUC001", "intake must be a JSON object")
    issues: list[Issue] = []

    sku_id = data.get("sku_id")
    if not isinstance(sku_id, str) or not sku_id:
        issues.append(Issue("AUC008", "sku_id must be a non-empty string"))
    elif sku_id not in catalog_skus:
        issues.append(Issue("AUC002", f"sku {sku_id!r} is not in the engine-generated catalog"))

    max_bids = _limit(data.get("max_bids", MAX_BIDS), "max_bids", issues)
    max_slots = _limit(data.get("max_slots", MAX_SLOTS), "max_slots", issues)

    slots: list[str] = []
    slots_raw = data.get("slots")
    if not isinstance(slots_raw, list) or not slots_raw:
        issues.append(Issue("AUC010", "slots must be a non-empty list of slot names"))
    else:
        for slot in slots_raw:
            if not isinstance(slot, str) or not slot:
                issues.append(Issue("AUC010", f"slot name must be a non-empty string, got {slot!r}"))
            elif slot in slots:
                issues.append(Issue("AUC005", f"duplicate slot {slot!r} in the auction slot list"))
            else:
                slots.append(slot)
        if len(slots) > max_slots:
            issues.append(Issue("AUC007", f"auction offers {len(slots)} slots; limit is {max_slots}"))
    slot_set = frozenset(slots)

    bids: list[Bid] = []
    seen_ids: set[str] = set()
    bids_raw = data.get("bids")
    if not isinstance(bids_raw, list):
        issues.append(Issue("AUC001", "bids must be a list"))
    else:
        if len(bids_raw) > max_bids:
            issues.append(Issue("AUC007", f"{len(bids_raw)} bids exceed the limit of {max_bids}"))
        for index, item in enumerate(bids_raw):
            bid = _validate_bid(item, index, slot_set, seen_ids, issues)
            if bid is not None:
                bids.append(bid)

    contracts = _validate_contracts(data.get("contracts"), issues)

    if issues:
        raise IntakeValidationError(issues)
    return Intake(
        sku_id=sku_id,
        slots=tuple(slots),
        bids=tuple(sorted(bids, key=lambda bid: bid.bid_id)),
        contracts=tuple(contracts),
        max_bids=max_bids,
        max_slots=max_slots,
    )


# ---------------------------------------------------------------------------
# Exact bounded allocation
# ---------------------------------------------------------------------------


def _is_better(candidate: Allocation, incumbent: Allocation) -> bool:
    if candidate.total_value != incumbent.total_value:
        return candidate.total_value > incumbent.total_value
    if candidate.advertiser_count != incumbent.advertiser_count:
        return candidate.advertiser_count < incumbent.advertiser_count
    return candidate.winner_ids < incumbent.winner_ids


def optimal_allocation(bids: Sequence[Bid]) -> Allocation:
    """Exhaustively search every bid subset (at most 2**8) for the optimum.

    A subset is feasible when no two bids share a slot and no two bids share
    an advertiser. Ties follow ``TIE_BREAK_RULES``; the result is independent
    of the input ordering.
    """
    ordered = sorted(bids, key=lambda bid: bid.bid_id)
    count = len(ordered)
    best = Allocation(winners=(), total_value=0)
    feasible = 0
    for mask in range(1 << count):
        chosen = tuple(ordered[i] for i in range(count) if mask & (1 << i))
        seen_slots: set[str] = set()
        seen_advertisers: set[str] = set()
        legal = True
        for bid in chosen:
            if bid.advertiser in seen_advertisers or seen_slots.intersection(bid.slots):
                legal = False
                break
            seen_advertisers.add(bid.advertiser)
            seen_slots.update(bid.slots)
        if not legal:
            continue
        feasible += 1
        candidate = Allocation(winners=chosen, total_value=sum(bid.value for bid in chosen))
        if _is_better(candidate, best):
            best = candidate
    return Allocation(winners=best.winners, total_value=best.total_value, feasible_count=feasible)


# ---------------------------------------------------------------------------
# Settlement contracts
# ---------------------------------------------------------------------------


def externality_payment(counterfactual_value: int | float, others_original_value: int | float) -> int | float:
    """VCG payment: harm a winner's presence causes the other advertisers.

    A negative result means the certificate is inconsistent; it must be
    invalidated, never silently clamped to zero.
    """
    payment = counterfactual_value - others_original_value
    if payment < 0:
        raise AuctionError(
            "AUC301",
            f"negative externality payment {payment}: counterfactual optimum "
            f"{counterfactual_value} is below the other winners' original value "
            f"{others_original_value}; the certificate must be invalidated, not clamped",
        )
    return payment


def settle_first_price(allocation: Allocation) -> dict[str, Any]:
    """Every winning bid pays its own value."""
    per_bid = [
        {"bid_id": bid.bid_id, "advertiser": bid.advertiser, "value": bid.value, "payment": bid.value}
        for bid in allocation.winners
    ]
    invoices = [
        {
            "advertiser": advertiser,
            "payment": sum(bid.value for bid in allocation.winners if bid.advertiser == advertiser),
        }
        for advertiser in sorted({bid.advertiser for bid in allocation.winners})
    ]
    return {
        "payment_rule": PAYMENT_RULES[FIRST_PRICE],
        "valid": True,
        "per_bid": per_bid,
        "invoices": invoices,
        "seller_revenue": sum(item["payment"] for item in per_bid),
    }


def settle_externality(bids: Sequence[Bid], allocation: Allocation) -> dict[str, Any]:
    """Every winning advertiser pays its VCG externality on the others.

    The counterfactual removes *all* bids of the advertiser -- including
    non-winning alternatives -- before re-solving the exact allocation.
    """
    invoices: list[dict[str, Any]] = []
    for advertiser in sorted({bid.advertiser for bid in allocation.winners}):
        winning = [bid for bid in allocation.winners if bid.advertiser == advertiser]
        removed = sorted(bid.bid_id for bid in bids if bid.advertiser == advertiser)
        remaining = [bid for bid in bids if bid.advertiser != advertiser]
        counterfactual = optimal_allocation(remaining)
        others_original = allocation.total_value - sum(bid.value for bid in winning)
        payment = externality_payment(counterfactual.total_value, others_original)
        invoices.append(
            {
                "advertiser": advertiser,
                "winning_bids": [bid.bid_id for bid in winning],
                "removed_bids": removed,
                "counterfactual": {
                    "winners": list(counterfactual.winner_ids),
                    "total_value": counterfactual.total_value,
                },
                "others_original_value": others_original,
                "payment": payment,
            }
        )
    return {
        "payment_rule": PAYMENT_RULES[EXTERNALITY_PRICE],
        "valid": True,
        "invoices": invoices,
        "seller_revenue": sum(invoice["payment"] for invoice in invoices),
    }


# ---------------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------------


def _atomic_preview(bids: Sequence[Bid], slots: Sequence[str]) -> tuple[list[str], int | float]:
    """Legacy single-slot greedy baseline, kept for comparison only."""
    free = set(slots)
    winners: list[str] = []
    total: int | float = 0
    for bid in sorted(bids, key=lambda item: (-item.value, item.bid_id)):
        if len(bid.slots) != 1:
            continue
        slot = bid.slots[0]
        if slot in free:
            free.remove(slot)
            winners.append(bid.bid_id)
            total += bid.value
    return winners, total


def _advertiser_bids(bids: Sequence[Bid]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for bid in bids:
        grouped.setdefault(bid.advertiser, []).append(bid.bid_id)
    return {advertiser: sorted(ids) for advertiser, ids in sorted(grouped.items())}


def _rejections(bids: Sequence[Bid], allocation: Allocation) -> list[dict[str, str]]:
    winner_ids = set(allocation.winner_ids)
    slot_owner = {slot: bid.bid_id for bid in allocation.winners for slot in bid.slots}
    advertiser_winner = {bid.advertiser: bid.bid_id for bid in allocation.winners}
    rejected: list[dict[str, str]] = []
    for bid in bids:
        if bid.bid_id in winner_ids:
            continue
        if bid.advertiser in advertiser_winner:
            reason = "advertiser_mutex"
            detail = (
                f"{bid.advertiser} already won {advertiser_winner[bid.advertiser]}; "
                "alternative bids from one advertiser are mutually exclusive"
            )
        else:
            conflicts = [(slot, slot_owner[slot]) for slot in bid.slots if slot in slot_owner]
            if conflicts:
                reason = "slot_conflict"
                detail = "; ".join(f"slot {slot} sold to {owner}" for slot, owner in conflicts)
            else:
                reason = "tie_break"
                detail = (
                    f"value {bid.value} adds nothing the tie-break rules would keep "
                    "(fewer winning advertisers preferred)"
                )
        rejected.append(
            {"bid_id": bid.bid_id, "advertiser": bid.advertiser, "reason": reason, "detail": detail}
        )
    return rejected


def build_certificate(
    data: Any,
    *,
    catalog: CatalogView,
    fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one auction round and emit its reviewable certificate.

    The certificate binds the engine catalog identity (dataset, blueprint
    hash, products fingerprint, verified SKU), the optimal allocation with
    per-slot winners and rejection reasons, and every authorized settlement
    contract derived from that same allocation.
    """
    intake = validate_intake(data, catalog_skus=catalog.skus)
    allocation = optimal_allocation(intake.bids)
    preview_winners, preview_value = _atomic_preview(intake.bids, intake.slots)

    slot_winners = {slot: None for slot in intake.slots}
    for bid in allocation.winners:
        for slot in bid.slots:
            slot_winners[slot] = bid.bid_id

    certificate: dict[str, Any] = {
        "certificate_version": 1,
        "valid": True,
        "invalid_reasons": [],
        "fixture": dict(fixture) if fixture else {},
        "catalog": {
            "dataset": catalog.dataset,
            "blueprint_sha256": catalog.blueprint_sha256,
            "products_fingerprint": catalog.fingerprint,
            "sku_id": intake.sku_id,
            "sku_verified": True,
        },
        "auction": {
            "slots": list(intake.slots),
            "limits": {"max_bids": intake.max_bids, "max_slots": intake.max_slots},
            "tie_breaks": list(TIE_BREAK_RULES),
            "advertiser_bids": _advertiser_bids(intake.bids),
        },
        "preview_baseline": {
            "winners": preview_winners,
            "total_value": preview_value,
            "optimal_gain": allocation.total_value - preview_value,
        },
        "allocation": {
            "winners": list(allocation.winner_ids),
            "total_value": allocation.total_value,
            "advertiser_count": allocation.advertiser_count,
            "feasible_allocations": allocation.feasible_count,
            "slot_winners": slot_winners,
            "advertiser_winners": {bid.advertiser: bid.bid_id for bid in allocation.winners},
            "rejected_bids": _rejections(intake.bids, allocation),
        },
        "settlements": {},
    }
    for name in intake.contracts:
        if name == FIRST_PRICE:
            certificate["settlements"][name] = settle_first_price(allocation)
        elif name == EXTERNALITY_PRICE:
            try:
                certificate["settlements"][name] = settle_externality(intake.bids, allocation)
            except AuctionError as exc:
                certificate["valid"] = False
                certificate["invalid_reasons"].append(exc.message)
                certificate["settlements"][name] = {
                    "payment_rule": PAYMENT_RULES[EXTERNALITY_PRICE],
                    "valid": False,
                    "error": exc.message,
                }
    return certificate
