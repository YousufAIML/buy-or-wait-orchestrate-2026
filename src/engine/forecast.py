"""Deterministic 90-day cash-flow forecast and payment safety checks.

This module imports only deterministic engine code. It never imports extraction,
provider, or network modules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .currency import CurrencyConverter
from .models import FinancialEvent, User
from .overrides import ResolvedOverrides
from .resolver import ResolvedEvent, resolve_event_lifecycles


class MissingForecastAmount(ValueError):
    """Raised instead of treating a blank financial amount as zero."""


@dataclass(frozen=True, slots=True)
class ForecastEvent:
    event_id: str
    event_date: date
    amount: Decimal
    direction: str
    category: str
    source: str
    recurring: bool = False

    @property
    def signed_amount(self) -> Decimal:
        if self.direction == "credit":
            return self.amount
        if self.direction == "debit":
            return -self.amount
        raise ValueError(f"unsupported cash direction: {self.direction!r}")


@dataclass(frozen=True, slots=True)
class RecurringRule:
    source_event_id: str
    category: str
    direction: str
    period_days: int
    amount: Decimal
    last_observed_date: date


@dataclass(frozen=True, slots=True)
class Forecast:
    request_date: date
    end_date: date
    initial_balance: Decimal
    minimum_balance: Decimal
    events_by_date: Dict[date, Tuple[ForecastEvent, ...]]
    balances_without_request: Dict[date, Decimal]
    recurring_rules: Tuple[RecurringRule, ...] = ()

    def _balance_walk(self, payment_date: Optional[date], payment_amount: Decimal) -> Dict[date, Decimal]:
        if payment_amount < 0:
            raise ValueError("payment amount cannot be negative")
        balance = self.initial_balance
        balances: Dict[date, Decimal] = {}
        day = self.request_date
        while day <= self.end_date:
            # Treat the requested payment as the first debit of its day. This
            # is conservative when a payment and a future cash event share a date.
            if payment_date == day:
                balance -= payment_amount
            for event in self.events_by_date.get(day, ()):
                balance += event.signed_amount
            balances[day] = balance
            day += timedelta(days=1)
        return balances

    def is_payment_safe(self, payment_date: date, payment_amount: Decimal) -> bool:
        """Check safety from payment_date through the end of the 90-day window."""
        if payment_date < self.request_date or payment_date > self.end_date:
            return False
        balances = self._balance_walk(payment_date, payment_amount)
        return all(
            balance >= self.minimum_balance
            for day, balance in balances.items()
            if day >= payment_date
        )

    def is_payment_plan_safe(
        self,
        payments: Iterable[tuple[date, Decimal]],
    ) -> bool:
        """Check a chronological multi-payment plan across the full horizon."""
        payment_by_date: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
        for payment_date, payment_amount in payments:
            if payment_amount < 0:
                return False
            if payment_date < self.request_date or payment_date > self.end_date:
                return False
            payment_by_date[payment_date] += payment_amount

        balance = self.initial_balance
        day = self.request_date
        while day <= self.end_date:
            balance -= payment_by_date.get(day, Decimal("0"))
            for event in self.events_by_date.get(day, ()):
                balance += event.signed_amount
            if balance < self.minimum_balance:
                return False
            day += timedelta(days=1)
        return True

    def amount_safe_to_pay(self, requested_amount: Decimal) -> Decimal:
        """Return the largest safe request-date payment before optional changes."""
        if requested_amount < 0:
            raise ValueError("requested amount cannot be negative")
        if self.initial_balance < self.minimum_balance:
            return Decimal("0")
        lowest_unpaid_balance = min(self.balances_without_request.values())
        capacity = min(
            self.initial_balance - self.minimum_balance,
            lowest_unpaid_balance - self.minimum_balance,
        )
        if capacity <= 0:
            return Decimal("0")
        return min(requested_amount, capacity)

    def earliest_date_for_full_payment(self, requested_amount: Decimal) -> Optional[date]:
        """Return the first date a one-time full payment passes the safety check."""
        if requested_amount < 0:
            raise ValueError("requested amount cannot be negative")
        day = self.request_date
        while day <= self.end_date:
            if self.is_payment_safe(day, requested_amount):
                return day
            day += timedelta(days=1)
        return None


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)


def _effective_date(event: FinancialEvent) -> date:
    return event.settlement_date or event.event_date


def _infer_period(dates: list[date]) -> Optional[int]:
    ordered = sorted(set(dates))
    if len(ordered) < 3:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(ordered, ordered[1:])]
    period = _median_int(gaps)
    if period <= 0:
        return None
    tolerance = max(2, round(period * 0.15))
    if max(gaps) - min(gaps) > tolerance:
        return None
    return period


def _calendar_subsequence(
    observations: list[tuple[ResolvedEvent, date, Decimal]],
) -> Optional[tuple[list[tuple[ResolvedEvent, date, Decimal]], int]]:
    """Find a clean calendar recurrence inside a mixed transaction group.

    A regular salary can share its category with an irregular bonus. Looking at
    the complete date group would reject both streams. Candidate subsequences
    use a tolerant day-of-month signature for monthly events or a tolerant
    weekday signature for weekly events. The tolerance covers a short
    weekend/holiday settlement shift without allowing a different cadence.
    """
    if len(observations) < 3:
        return None

    ordered = sorted(observations, key=lambda item: item[1])
    candidates: list[tuple[list[tuple[ResolvedEvent, date, Decimal]], int]] = []
    seen: set[tuple[str, ...]] = set()

    def add_candidate(
        candidate: list[tuple[ResolvedEvent, date, Decimal]],
        *,
        minimum_gap: int,
        maximum_gap: int,
    ) -> None:
        candidate = sorted(candidate, key=lambda item: item[1])
        if len(candidate) < 3:
            return
        dates = [item[1] for item in candidate]
        if len(set(dates)) != len(dates):
            return
        gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
        if not gaps or not all(minimum_gap <= gap <= maximum_gap for gap in gaps):
            return
        period = _median_int(gaps)
        tolerance = max(2, round(period * 0.2))
        if any(abs(gap - period) > tolerance for gap in gaps):
            return
        key = tuple(item[0].event.event_id for item in candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append((candidate, period))

    # Monthly candidates preserve one occurrence per calendar month. A small
    # day-of-month tolerance covers a payroll date moved off a weekend/holiday.
    for anchor_day in range(1, 32):
        candidate = [
            item
            for item in ordered
            if abs(item[1].day - anchor_day) <= 3
        ]
        month_keys = [item[1].year * 12 + item[1].month for item in candidate]
        if len(set(month_keys)) != len(month_keys):
            continue
        if len(month_keys) >= 3 and all(
            later - earlier == 1 for earlier, later in zip(sorted(month_keys), sorted(month_keys)[1:])
        ):
            add_candidate(candidate, minimum_gap=24, maximum_gap=36)

    # Weekly candidates preserve one weekday, with a two-day tolerance for a
    # weekend/holiday shift. Five-to-nine-day gaps still distinguish weekly
    # cadence from a biweekly or monthly stream.
    for anchor_weekday in range(7):
        candidate = [
            item
            for item in ordered
            if min(
                (item[1].weekday() - anchor_weekday) % 7,
                (anchor_weekday - item[1].weekday()) % 7,
            ) <= 2
        ]
        add_candidate(candidate, minimum_gap=5, maximum_gap=9)

    if not candidates:
        return None

    def score(
        candidate_with_period: tuple[list[tuple[ResolvedEvent, date, Decimal]], int],
    ) -> tuple[int, Decimal, int]:
        candidate, _period = candidate_with_period
        amounts = [item[2] for item in candidate]
        center = _median(amounts)
        dispersion = sum((abs(amount - center) for amount in amounts), Decimal("0"))
        return (len(candidate), -dispersion, candidate[-1][1].toordinal())

    return max(candidates, key=score)


def _amount_in_home(
    event: FinancialEvent,
    home_currency: str,
    converter: CurrencyConverter,
    overrides: Mapping[str, Decimal],
    at_date: date,
) -> Decimal:
    amount = overrides.get(event.event_id, event.amount)
    if amount is None:
        raise MissingForecastAmount(f"event {event.event_id} has no resolved amount")
    return converter.convert(amount, event.currency, home_currency, at_date)


def _make_explicit_event(
    resolved: ResolvedEvent,
    user: User,
    request_date: date,
    end_date: date,
    converter: CurrencyConverter,
    overrides: Mapping[str, Decimal],
) -> Optional[ForecastEvent]:
    event = resolved.event
    due_date = _effective_date(event)
    if not resolved.included_in_cash_forecast or not (request_date < due_date <= end_date):
        return None
    if event.direction not in {"debit", "credit"}:
        return None
    return ForecastEvent(
        event_id=event.event_id,
        event_date=due_date,
        amount=_amount_in_home(event, user.home_currency, converter, overrides, due_date),
        direction=event.direction,
        category=event.category,
        source="explicit",
        recurring=False,
    )


def _recurrence_streams(
    observations: list[tuple[ResolvedEvent, date, Decimal]],
) -> list[tuple[list[tuple[ResolvedEvent, date, Decimal]], int]]:
    """Choose one full cadence or split into independently regular streams."""
    ordered = sorted(observations, key=lambda item: item[1])
    full_period = _infer_period([observed_date for _, observed_date, _ in ordered])
    clean_subsequence = _calendar_subsequence(ordered)
    if clean_subsequence is None:
        return [] if full_period is None else [(ordered, full_period)]

    candidate, candidate_period = clean_subsequence
    candidate_ids = {resolved.event.event_id for resolved, _, _ in candidate}
    remainder = [
        item
        for item in ordered
        if item[0].event.event_id not in candidate_ids
    ]
    remainder_period = _infer_period([observed_date for _, observed_date, _ in remainder])

    if full_period is not None and (
        candidate_period != full_period
        or len(candidate) * 2 <= len(ordered)
        or remainder_period is not None
    ):
        # The complete group is already a coherent cadence. Prefer it when it
        # is denser than a sparse calendar-signature subset or when the proposed
        # remainder is itself regular.
        return [(ordered, full_period)]

    if remainder_period is not None:
        # A regular remainder is a second stream, not an irregular outlier.
        # Preserve both streams instead of discarding the remainder or letting
        # the calendar candidate hide a denser cadence.
        return [(candidate, candidate_period), (remainder, remainder_period)]

    # Only use the calendar split when the excluded rows are genuinely
    # irregular (fewer than three regular observations or no stable cadence).
    return [(candidate, candidate_period)]


def _recurring_rules(
    resolved_events: Iterable[ResolvedEvent],
    user: User,
    request_date: date,
    converter: CurrencyConverter,
    overrides: Mapping[str, Decimal],
) -> Tuple[RecurringRule, ...]:
    groups: dict[tuple[str, str, str, str], list[tuple[ResolvedEvent, date, Decimal]]] = defaultdict(list)
    for resolved in resolved_events:
        event = resolved.event
        observed_date = _effective_date(event)
        if (
            not resolved.included_in_cash_forecast
            or event.status != "settled"
            or observed_date >= request_date
            or event.event_type not in {"expense", "subscription", "income", "debt_payment"}
            or event.direction not in {"debit", "credit"}
        ):
            continue
        amount = _amount_in_home(event, user.home_currency, converter, overrides, observed_date)
        key = (event.event_type, event.category, event.direction, event.currency)
        groups[key].append((resolved, observed_date, amount))

    rules: list[RecurringRule] = []
    for group in groups.values():
        for observations, period in _recurrence_streams(group):
            last_date = observations[-1][1]
            # Do not carry a stale historical pattern into a request with no
            # recent evidence of continuation.
            if last_date < request_date - timedelta(days=period * 2):
                continue
            amounts = [amount for _, _, amount in observations]
            latest = observations[-1][0].event
            # Fixed commitments use a central amount; protected variable
            # expenses use the recent maximum so essential spending is not
            # under-reserved.
            if latest.direction == "debit" and (
                latest.flexibility != "fixed" or latest.category in user.expense_categories_to_protect
            ):
                amount = max(amounts[-3:])
            else:
                amount = _median(amounts[-3:])
            # Outlier guard: a one-off purchase landing as the latest stream
            # observation must not inflate the projected recurring amount.
            # If the inferred amount exceeds 2x the median of the stream's
            # prior observations (minimum 4 observations), fall back to that
            # prior median.
            if len(amounts) >= 4:
                prior_median = _median(amounts[:-1])
                if prior_median > 0 and amount > 2 * prior_median:
                    amount = prior_median
            rules.append(
                RecurringRule(
                    source_event_id=latest.event_id,
                    category=latest.category,
                    direction=latest.direction,
                    period_days=period,
                    amount=amount,
                    last_observed_date=last_date,
                )
            )
    return tuple(sorted(rules, key=lambda rule: (rule.last_observed_date, rule.source_event_id)))


def build_forecast(
    user: User,
    events: Iterable[FinancialEvent],
    converter: CurrencyConverter,
    request_date: date,
    *,
    horizon_days: int = 90,
    resolved_overrides: Optional[ResolvedOverrides] = None,
) -> Forecast:
    """Build a deterministic request_date..request_date+90 balance forecast."""
    if horizon_days < 0:
        raise ValueError("horizon_days cannot be negative")
    if user.current_available_balance < 0 or user.minimum_balance_to_keep < 0:
        raise ValueError("balances and minimum must be non-negative")
    end_date = request_date + timedelta(days=horizon_days)
    overrides = resolved_overrides.image_amounts_by_event_id if resolved_overrides else {}
    resolution = resolve_event_lifecycles(events)

    explicit: list[ForecastEvent] = []
    for resolved in resolution.events:
        item = _make_explicit_event(
            resolved, user, request_date, end_date, converter, overrides
        )
        if item is not None:
            explicit.append(item)

    rules = _recurring_rules(resolution.events, user, request_date, converter, overrides)
    explicit_keys = {(item.event_date, item.category, item.direction) for item in explicit}
    projected: list[ForecastEvent] = []
    for rule in rules:
        next_date = rule.last_observed_date + timedelta(days=rule.period_days)
        while next_date <= end_date:
            key = (next_date, rule.category, rule.direction)
            if next_date > request_date and key not in explicit_keys:
                projected.append(
                    ForecastEvent(
                        event_id=f"recurring:{rule.source_event_id}:{next_date.isoformat()}",
                        event_date=next_date,
                        amount=rule.amount,
                        direction=rule.direction,
                        category=rule.category,
                        source="recurring",
                        recurring=True,
                    )
                )
            next_date += timedelta(days=rule.period_days)

    grouped: dict[date, list[ForecastEvent]] = defaultdict(list)
    for item in explicit + projected:
        grouped[item.event_date].append(item)
    events_by_date = {
        day: tuple(sorted(items, key=lambda item: (item.direction, item.event_id)))
        for day, items in grouped.items()
    }
    no_payment = Forecast(
        request_date=request_date,
        end_date=end_date,
        initial_balance=user.current_available_balance,
        minimum_balance=user.minimum_balance_to_keep,
        events_by_date=events_by_date,
        balances_without_request={},
        recurring_rules=rules,
    )
    balances_without_request = no_payment._balance_walk(None, Decimal("0"))
    return Forecast(
        request_date=request_date,
        end_date=end_date,
        initial_balance=user.current_available_balance,
        minimum_balance=user.minimum_balance_to_keep,
        events_by_date=events_by_date,
        balances_without_request=balances_without_request,
        recurring_rules=rules,
    )
