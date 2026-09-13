"""Deterministic linked-event lifecycle resolution.

A linked chain can contain several legitimate cash legs (for example an expense
and a later refund), so resolution annotates each source row instead of blindly
collapsing a chain to one amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from .models import FinancialEvent


@dataclass(frozen=True, slots=True)
class ResolvedEvent:
    event: FinancialEvent
    included_in_cash_forecast: bool
    canonical_event_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    events: Tuple[ResolvedEvent, ...]
    conflicts: Tuple[str, ...]

    @property
    def by_event_id(self) -> Dict[str, ResolvedEvent]:
        return {resolved.event.event_id: resolved for resolved in self.events}


_SOURCE_MARKERS = (
    "employer",
    "card",
    "merchant",
    "investment",
    "bill",
    "salary",
    "rent",
)


def _source_key(event: FinancialEvent) -> str:
    description = event.description.lower()
    for marker in _SOURCE_MARKERS:
        if marker in description:
            return marker
    return "unknown"


def _explicit_strength(event: FinancialEvent) -> int:
    text = event.description.lower()
    if event.status == "cancelled" or "cancel" in text:
        return 4
    if "amend" in text or "revis" in text or "update" in text:
        return 4
    if event.status == "settled" or "settled" in text:
        return 3
    return 0


def _is_duplicate_description(event: FinancialEvent) -> bool:
    text = event.description.lower()
    return "duplicate" in text or "authorization" in text


def _root_id(event: FinancialEvent, by_id: Dict[str, FinancialEvent]) -> str:
    current = event
    visited = set()
    while current.linked_event_id and current.linked_event_id in by_id:
        if current.event_id in visited:
            raise ValueError(f"cycle in linked event chain at {current.event_id}")
        visited.add(current.event_id)
        current = by_id[current.linked_event_id]
    return current.event_id


def _base_inclusion(event: FinancialEvent) -> tuple[bool, str]:
    if event.direction == "non_cash":
        return False, "excluded: non_cash event"
    if event.status == "cancelled":
        return False, "rule 1: explicit cancellation excludes the event"
    if event.status == "failed":
        return False, "rule 1: failed event is not a cash movement"
    if event.status == "unrealized":
        return False, "excluded: unrealized value is not available cash"
    if event.status == "pending" and event.direction == "credit":
        return False, "excluded: pending credit is not confirmed income"
    if event.status == "settled":
        return True, "included: settled cash movement"
    if event.status == "scheduled" and event.direction == "debit":
        return True, "included: scheduled debit must be reserved"
    if (
        event.status == "scheduled"
        and event.direction == "credit"
        and event.event_type == "income"
        and event.category == "salary"
        and "confirmed" in event.description.lower()
    ):
        return True, "included: scheduled confirmed salary on settlement date"
    if event.status == "pending" and event.direction == "debit":
        return True, "included: pending debit must be reserved"
    return False, f"excluded: unsupported cash status {event.status!r}"


def resolve_event_lifecycles(events: Iterable[FinancialEvent]) -> ResolutionResult:
    """Apply the documented conflict order and return auditable decisions."""
    materialized = tuple(events)
    by_id = {event.event_id: event for event in materialized}
    if len(by_id) != len(materialized):
        raise ValueError("event_id must be unique before lifecycle resolution")

    groups: Dict[str, List[FinancialEvent]] = {}
    for event in materialized:
        groups.setdefault(_root_id(event, by_id), []).append(event)

    decisions: Dict[str, ResolvedEvent] = {}
    conflicts: List[str] = []

    for root, group in groups.items():
        group = sorted(group, key=lambda event: (event.event_date, event.event_id))
        if len(group) > 1:
            conflicts.append(f"chain {root}: evaluating {len(group)} linked records")

        superseded_by: Dict[str, str] = {}
        for event in group:
            parent = by_id.get(event.linked_event_id or "")
            if not parent:
                continue
            child_text = event.description.lower()
            parent_text = parent.description.lower()
            # A settled purchase replaces its authorization representation; the
            # authorization is not a second debit.
            if (
                event.status == "settled"
                and event.direction == parent.direction
                and "authorization" in parent_text
                and "refund" not in child_text
                and "sale proceeds" not in child_text
            ):
                superseded_by[parent.event_id] = event.event_id

            # A later settled record is the explicit winner over a linked
            # estimate/forecast from the same semantic source.
            if (
                event.status == "settled"
                and _source_key(event) == _source_key(parent)
                and event.direction == parent.direction
                and event.category == parent.category
                and any(word in parent_text for word in ("estimate", "forecast", "possible"))
            ):
                superseded_by[parent.event_id] = event.event_id

        # A pending possible duplicate loses to an already settled original.
        for event in group:
            if not _is_duplicate_description(event):
                continue
            winner = next(
                (
                    other
                    for other in group
                    if other.event_id != event.event_id
                    and other.status == "settled"
                    and other.direction == event.direction
                    and other.category == event.category
                    and other.event_date <= event.event_date
                ),
                None,
            )
            if winner:
                superseded_by[event.event_id] = winner.event_id

        # If the chain remains ambiguous, the newer explicit same-source record
        # wins; this is the documented safer interpretation for duplicates.
        for older in group:
            for newer in group:
                if newer.event_date <= older.event_date:
                    continue
                if _source_key(newer) != _source_key(older):
                    continue
                if newer.direction != older.direction or newer.category != older.category:
                    continue
                if _explicit_strength(newer) > _explicit_strength(older):
                    superseded_by[older.event_id] = newer.event_id

        for event in group:
            included, reason = _base_inclusion(event)
            canonical = event.event_id
            if event.event_id in superseded_by:
                canonical = superseded_by[event.event_id]
                winner = by_id[canonical]
                included = False
                reason = (
                    f"rule 1/2: record superseded by explicit newer record "
                    f"{winner.event_id} ({winner.status})"
                )
            elif included and event.status == "settled":
                # Rule 3 is reflected in the duplicate checks above; keep a
                # clear audit phrase for a settled winner in a conflicted chain.
                if len(group) > 1:
                    reason = "rule 3: settled interpretation retained over estimates/pending duplicates"

            if len(group) > 1:
                conflicts.append(f"{event.event_id}: {reason}")
            decisions[event.event_id] = ResolvedEvent(event, included, canonical, reason)

    return ResolutionResult(
        events=tuple(decisions[event.event_id] for event in materialized),
        conflicts=tuple(conflicts),
    )
