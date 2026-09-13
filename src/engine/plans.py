"""Deterministic Phase 4 payment-plan construction and ranking.

This module builds safe payment candidates on top of the Phase 3 forecast. It
contains no provider, extraction, or output-assembly logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .currency import CurrencyConverter
from .forecast import Forecast, ForecastEvent
from .models import FinancialEvent, PaymentOption, Request, User


@dataclass(frozen=True, slots=True)
class PlanPayment:
    payment_date: date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SpendingChange:
    action: str
    event_id: str
    new_amount: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.action not in {"stop", "reduce_to"}:
            raise ValueError(f"unsupported spending-change action: {self.action!r}")
        if self.action == "stop" and self.new_amount is not None:
            raise ValueError("stop changes cannot carry a new amount")
        if self.action == "reduce_to" and (self.new_amount is None or self.new_amount < 0):
            raise ValueError("reduce_to changes require a non-negative new amount")

    @property
    def token(self) -> str:
        if self.action == "stop":
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{_format_amount(self.new_amount or Decimal('0'))}"


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    method: str
    payments: Tuple[PlanPayment, ...]
    total_paid: Decimal
    payment_option_id: str
    spending_changes: Tuple[SpendingChange, ...] = ()
    safe: bool = True
    completes_by_deadline: bool = False

    @property
    def start_date(self) -> date:
        return min(payment.payment_date for payment in self.payments)

    @property
    def payment_count(self) -> int:
        return len(self.payments)

    @property
    def payment_plan(self) -> str:
        return "|".join(
            f"{payment.payment_date.isoformat()}:{_format_amount(payment.amount)}"
            for payment in sorted(self.payments, key=lambda item: item.payment_date)
        )

    @property
    def spending_changes_needed(self) -> str:
        if not self.spending_changes:
            return "none"
        return "|".join(change.token for change in self.spending_changes)


@dataclass(frozen=True, slots=True)
class PlanDecision:
    amount_safe_to_pay: Decimal
    earliest_date_for_full_payment: Optional[date]
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    spending_changes_needed: str
    selected_candidate: Optional[PlanCandidate]
    candidates: Tuple[PlanCandidate, ...] = ()


def _format_amount(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _option_id(options: Sequence[PaymentOption], method: str) -> str:
    matching = sorted(option.payment_option_id for option in options if option.payment_method == method)
    return matching[0] if matching else method


def _installment_payments(option: PaymentOption) -> Optional[Tuple[PlanPayment, ...]]:
    if option.number_of_payments <= 0 or option.payment_frequency_days is None:
        return None
    return tuple(
        PlanPayment(
            option.first_payment_date + timedelta(days=option.payment_frequency_days * index),
            option.payment_amount,
        )
        for index in range(option.number_of_payments)
    )


def _payment_option_candidates(
    user: User,
    request: Request,
    options: Sequence[PaymentOption],
    forecast: Forecast,
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    methods = set(user.payment_methods_user_will_consider)
    full_options = [option for option in options if option.payment_method == "full_payment"]

    if "full_payment" in methods:
        for option in full_options:
            candidates.append(
                PlanCandidate(
                    method="full_payment",
                    payments=(PlanPayment(request.request_date, request.requested_amount),),
                    total_paid=option.total_payable_amount,
                    payment_option_id=option.payment_option_id,
                )
            )

    safe_today = forecast.amount_safe_to_pay(request.requested_amount)
    earliest = forecast.earliest_date_for_full_payment(request.requested_amount)
    if (
        request.allows_partial_payment
        and "partial_payment" in methods
        and Decimal("0") < safe_today < request.requested_amount
        and earliest is not None
    ):
        candidates.append(
            PlanCandidate(
                method="partial_payment",
                payments=(
                    PlanPayment(request.request_date, safe_today),
                    PlanPayment(earliest, request.requested_amount - safe_today),
                ),
                total_paid=request.requested_amount,
                payment_option_id="partial_payment",
            )
        )

    if "installments" in methods and user.max_installment_months is not None:
        for option in options:
            if (
                option.payment_method != "installments"
                or option.number_of_payments > user.max_installment_months
            ):
                continue
            payments = _installment_payments(option)
            if payments is None:
                continue
            candidates.append(
                PlanCandidate(
                    method="installments",
                    payments=payments,
                    total_paid=option.total_payable_amount,
                    payment_option_id=option.payment_option_id,
                )
            )

    if "full_payment" in methods and earliest is not None and earliest > request.request_date:
        candidates.append(
            PlanCandidate(
                method="wait",
                payments=(PlanPayment(earliest, request.requested_amount),),
                total_paid=request.requested_amount,
                payment_option_id=_option_id(options, "full_payment"),
            )
        )
    return candidates


def _eligible_changes(
    user: User,
    forecast: Forecast,
    events: Iterable[FinancialEvent],
    converter: CurrencyConverter,
) -> Tuple[SpendingChange, ...]:
    event_by_id = {event.event_id: event for event in events}
    changes: list[SpendingChange] = []
    for rule in forecast.recurring_rules:
        if rule.direction != "debit":
            continue
        event = event_by_id.get(rule.source_event_id)
        if event is None:
            continue
        flexibility = event.flexibility or ""
        if flexibility not in {"reducible", "stoppable", "reducible_or_stoppable"}:
            continue

        if (
            event.category in user.expense_categories_user_is_willing_to_stop
            and flexibility in {"stoppable", "reducible_or_stoppable"}
        ):
            changes.append(SpendingChange("stop", event.event_id))

        if (
            event.category in user.expense_categories_user_is_willing_to_reduce
            and flexibility in {"reducible", "reducible_or_stoppable"}
            and event.minimum_allowed_amount is not None
        ):
            minimum_home = converter.convert(
                event.minimum_allowed_amount,
                event.currency,
                user.home_currency,
                rule.last_observed_date,
            )
            if minimum_home < rule.amount:
                changes.append(SpendingChange("reduce_to", event.event_id, minimum_home))
    return tuple(sorted(changes, key=lambda change: (change.event_id, change.action, change.token)))


def _adjust_forecast(forecast: Forecast, changes: Sequence[SpendingChange]) -> Forecast:
    by_event_id = {change.event_id: change for change in changes}
    adjusted_by_date: dict[date, Tuple[ForecastEvent, ...]] = {}
    for day, items in forecast.events_by_date.items():
        adjusted: list[ForecastEvent] = []
        for item in items:
            source_id = item.event_id.split(":", 2)[1] if item.recurring else None
            change = by_event_id.get(source_id or "")
            if change is None:
                adjusted.append(item)
                continue
            amount = Decimal("0") if change.action == "stop" else min(item.amount, change.new_amount or Decimal("0"))
            adjusted.append(
                ForecastEvent(
                    event_id=item.event_id,
                    event_date=item.event_date,
                    amount=amount,
                    direction=item.direction,
                    category=item.category,
                    source=item.source,
                    recurring=item.recurring,
                )
            )
        adjusted_by_date[day] = tuple(adjusted)

    adjusted_forecast = Forecast(
        request_date=forecast.request_date,
        end_date=forecast.end_date,
        initial_balance=forecast.initial_balance,
        minimum_balance=forecast.minimum_balance,
        events_by_date=adjusted_by_date,
        balances_without_request={},
        recurring_rules=forecast.recurring_rules,
    )
    return Forecast(
        request_date=adjusted_forecast.request_date,
        end_date=adjusted_forecast.end_date,
        initial_balance=adjusted_forecast.initial_balance,
        minimum_balance=adjusted_forecast.minimum_balance,
        events_by_date=adjusted_forecast.events_by_date,
        balances_without_request=adjusted_forecast._balance_walk(None, Decimal("0")),
        recurring_rules=adjusted_forecast.recurring_rules,
    )


def _rank_key(candidate: PlanCandidate) -> tuple[int, int, Decimal, date, int, str]:
    return (
        -int(candidate.completes_by_deadline),
        -int(not candidate.spending_changes),
        candidate.total_paid,
        candidate.start_date,
        candidate.payment_count,
        candidate.payment_option_id,
    )


def rank_plan_candidates(candidates: Iterable[PlanCandidate]) -> Tuple[PlanCandidate, ...]:
    """Apply the six contract ranking rules in deterministic order."""
    return tuple(sorted(candidates, key=_rank_key))


def _with_safety(
    candidate: PlanCandidate,
    forecast: Forecast,
    desired_completion_date: date,
    changes: Tuple[SpendingChange, ...],
) -> PlanCandidate:
    adjusted = _adjust_forecast(forecast, changes) if changes else forecast
    safe = adjusted.is_payment_plan_safe(
        tuple((payment.payment_date, payment.amount) for payment in candidate.payments)
    )
    completes = candidate.payments[-1].payment_date <= desired_completion_date
    return PlanCandidate(
        method=candidate.method,
        payments=candidate.payments,
        total_paid=candidate.total_paid,
        payment_option_id=candidate.payment_option_id,
        spending_changes=changes,
        safe=safe,
        completes_by_deadline=completes,
    )


def construct_plan(
    user: User,
    request: Request,
    options: Iterable[PaymentOption],
    events: Iterable[FinancialEvent],
    forecast: Forecast,
    converter: CurrencyConverter,
) -> PlanDecision:
    """Construct, safety-check, and rank eligible plans for one request."""
    materialized_options = tuple(options)
    materialized_events = tuple(events)
    base_candidates = _payment_option_candidates(user, request, materialized_options, forecast)
    changes = _eligible_changes(user, forecast, materialized_events, converter)

    evaluated: list[PlanCandidate] = []
    change_sets: list[Tuple[SpendingChange, ...]] = [()]
    for count in range(1, min(3, len(changes)) + 1):
        for combination in combinations(changes, count):
            if len({change.event_id for change in combination}) != len(combination):
                continue
            change_sets.append(tuple(combination))

    for base in base_candidates:
        for change_set in change_sets:
            evaluated.append(_with_safety(base, forecast, request.desired_completion_date, change_set))

    safe_by_deadline = [
        candidate
        for candidate in evaluated
        if candidate.safe and candidate.completes_by_deadline
    ]
    ranked = rank_plan_candidates(safe_by_deadline)
    selected = ranked[0] if ranked else None
    if selected is None:
        return PlanDecision(
            amount_safe_to_pay=forecast.amount_safe_to_pay(request.requested_amount),
            earliest_date_for_full_payment=forecast.earliest_date_for_full_payment(request.requested_amount),
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            spending_changes_needed="none",
            selected_candidate=None,
            candidates=tuple(evaluated),
        )

    if (
        selected.method == "full_payment"
        and selected.start_date == request.request_date
        and not selected.spending_changes
    ):
        status = "affordable_now"
    elif selected.method == "wait":
        status = "affordable_later"
    else:
        status = "affordable_with_plan"
    return PlanDecision(
        amount_safe_to_pay=forecast.amount_safe_to_pay(request.requested_amount),
        earliest_date_for_full_payment=forecast.earliest_date_for_full_payment(request.requested_amount),
        affordability_status=status,
        recommended_payment_method=selected.method,
        payment_plan=selected.payment_plan,
        spending_changes_needed=selected.spending_changes_needed,
        selected_candidate=selected,
        candidates=tuple(evaluated),
    )
