"""Phase 5 output assembly, explanation seams, and strict validation."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from .currency import CurrencyConverter
from .forecast import Forecast, MissingForecastAmount
from .models import FinancialEvent, PaymentOption, Request, User
from .plans import PlanDecision, construct_plan

OUTPUT_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)
ALLOWED_STATUSES = frozenset(
    {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
)
ALLOWED_METHODS = frozenset(
    {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
)
_PAYMENT_PART = re.compile(r"^(\d{4}-\d{2}-\d{2}):([0-9]+(?:\.[0-9]+)?)$")
_STOP_PART = re.compile(r"^stop:([^:|]+)$")
_REDUCE_PART = re.compile(r"^reduce_to:([^:|]+):([0-9]+(?:\.[0-9]+)?)$")


class OutputValidationError(ValueError):
    """Raised when a finalized output row violates the submission contract."""


def normalize_explanation(explanation: str) -> str:
    """Normalize only the absent-date fact token; never alter numeric decisions."""
    return re.sub(
        r"(?m)(earliest_full_payment=)(?:null|None|NULL)(?=[.;\s]|$)",
        r"\1none",
        explanation,
    )


class ExplanationGenerator(Protocol):
    def generate(self, request: Request, decision: PlanDecision) -> str:
        """Explain an already-finalized decision; never make or revise it."""


@dataclass(frozen=True, slots=True)
class OutputRow:
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str

    def csv_values(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "amount_safe_to_pay": _format_amount(self.amount_safe_to_pay),
            "affordability_status": self.affordability_status,
            "recommended_payment_method": self.recommended_payment_method,
            "payment_plan": self.payment_plan,
            "earliest_date_for_full_payment": (
                self.earliest_date_for_full_payment.isoformat()
                if self.earliest_date_for_full_payment is not None
                else ""
            ),
            "spending_changes_needed": self.spending_changes_needed,
            "decision_explanation": self.decision_explanation,
        }


def check_explanation_consistency(
    explanation: str,
    request: Request,
    decision: PlanDecision,
) -> tuple[str, ...]:
    """Check the exact labeled facts against the finalized numeric decision."""
    expected = {
        "safe_amount": _format_amount(decision.amount_safe_to_pay),
        "status": decision.affordability_status,
        "method": decision.recommended_payment_method,
        "payment_plan": decision.payment_plan,
        "earliest_full_payment": (
            decision.earliest_date_for_full_payment.isoformat()
            if decision.earliest_date_for_full_payment is not None
            else "none"
        ),
        "spending_changes": decision.spending_changes_needed,
    }
    flags: list[str] = []
    for key, value in expected.items():
        marker = f"{key}={value}"
        if marker not in explanation:
            flags.append(f"{key}: expected exact {value!r}")
    return tuple(flags)


class MockExplanationGenerator:
    """Deterministic test harness; it never imports or calls a provider."""

    def generate(self, request: Request, decision: PlanDecision) -> str:
        safe = _format_amount(decision.amount_safe_to_pay)
        if decision.recommended_payment_method == "not_recommended":
            return (
                f"No eligible safe plan completes this {request.requested_amount} "
                f"request by {request.desired_completion_date.isoformat()}. "
                f"Safe to pay on the request date before optional changes: {safe}."
            )
        plan = decision.payment_plan
        changes = decision.spending_changes_needed
        return (
            f"The request is {decision.affordability_status} using "
            f"{decision.recommended_payment_method}. The finalized payment plan is "
            f"{plan}; safe to pay on the request date before optional changes is {safe}. "
            f"Spending changes needed: {changes}."
        )


class GroqExplanationGenerator:
    """Future live explanation path; the prompt explicitly forbids decision-making."""

    def __init__(self, client: object):
        self.client = client

    def generate(self, request: Request, decision: PlanDecision) -> str:
        finalized = {
            "request_id": request.request_id,
            "requested_amount": str(request.requested_amount),
            "request_date": request.request_date.isoformat(),
            "desired_completion_date": request.desired_completion_date.isoformat(),
            "amount_safe_to_pay": str(decision.amount_safe_to_pay),
            "affordability_status": decision.affordability_status,
            "recommended_payment_method": decision.recommended_payment_method,
            "payment_plan": decision.payment_plan,
            "earliest_date_for_full_payment": (
                decision.earliest_date_for_full_payment.isoformat()
                if decision.earliest_date_for_full_payment is not None
                else None
            ),
            "spending_changes_needed": decision.spending_changes_needed,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "Write one concise plain-language financial explanation. The supplied "
                    "decision fields are final. Do not decide, rank, recalculate, correct, "
                    "or reinterpret any value. Do not introduce a payment, date, amount, "
                    "or spending change not present in the supplied fields. Start with "
                    "the exact finalized facts line requested by the user message."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Explain this finalized decision only. Start with exactly this labeled "
                    "line, preserving every value exactly: Facts: safe_amount=<value>; "
                    "status=<value>; method=<value>; payment_plan=<value>; "
                    "earliest_full_payment=<value>; spending_changes=<value>. Use the "
                    "literal string none for an absent earliest date, never null. Then add "
                    "one concise plain-language sentence. Return explanation text only. "
                    "FINAL_DECISION=" + json.dumps(finalized, sort_keys=True)
                ),
            },
        ]
        response = self.client.chat_completion(messages, request_key=request.request_id)
        explanation = normalize_explanation(str(response.text).strip())
        if not explanation:
            raise OutputValidationError("Groq returned an empty decision explanation")
        return explanation


def _format_amount(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _parse_plan(value: str) -> tuple[tuple[date, Decimal], ...]:
    if value == "none":
        return ()
    if not value:
        raise OutputValidationError("payment_plan cannot be blank")
    parsed: list[tuple[date, Decimal]] = []
    previous: Optional[date] = None
    for part in value.split("|"):
        match = _PAYMENT_PART.fullmatch(part)
        if match is None:
            raise OutputValidationError(f"invalid payment_plan entry: {part!r}")
        try:
            payment_date = date.fromisoformat(match.group(1))
            amount = Decimal(match.group(2))
        except (InvalidOperation, ValueError) as exc:
            raise OutputValidationError(f"invalid payment_plan entry: {part!r}") from exc
        if not amount.is_finite() or amount <= 0:
            raise OutputValidationError("payment plan amounts must be positive finite decimals")
        if previous is not None and payment_date < previous:
            raise OutputValidationError("payment_plan entries must be chronological")
        previous = payment_date
        parsed.append((payment_date, amount))
    return tuple(parsed)


def _option_schedule(option: PaymentOption) -> tuple[tuple[date, Decimal], ...]:
    if option.number_of_payments <= 0 or option.payment_frequency_days is None:
        raise OutputValidationError(
            f"installment option {option.payment_option_id} lacks a valid schedule"
        )
    return tuple(
        (
            option.first_payment_date + timedelta(days=index * option.payment_frequency_days),
            option.payment_amount,
        )
        for index in range(option.number_of_payments)
    )


def _validate_spending_changes(
    value: str,
    *,
    user: User,
    events: Iterable[FinancialEvent],
    forecast: Forecast,
    converter: CurrencyConverter,
) -> None:
    if value == "none":
        return
    if not value:
        raise OutputValidationError("spending_changes_needed cannot be blank")
    parts = value.split("|")
    if len(parts) > 3:
        raise OutputValidationError("at most three spending changes are allowed")

    event_by_id = {event.event_id: event for event in events}
    rule_by_source = {rule.source_event_id: rule for rule in forecast.recurring_rules}
    seen: set[str] = set()
    for part in parts:
        stop = _STOP_PART.fullmatch(part)
        reduce_match = _REDUCE_PART.fullmatch(part)
        if stop is None and reduce_match is None:
            raise OutputValidationError(f"invalid spending change: {part!r}")
        event_id = (stop or reduce_match).group(1)
        if event_id in seen:
            raise OutputValidationError(
                f"stop/reduce actions are mutually exclusive per event: {event_id}"
            )
        seen.add(event_id)
        event = event_by_id.get(event_id)
        rule = rule_by_source.get(event_id)
        if event is None or rule is None or rule.direction != "debit":
            raise OutputValidationError(
                f"spending change must target a recurring debit event: {event_id}"
            )
        flexibility = event.flexibility or ""
        if event.category in user.expense_categories_to_protect:
            raise OutputValidationError(f"protected category cannot be changed: {event.category}")
        if stop is not None:
            if event.category not in user.expense_categories_user_is_willing_to_stop:
                raise OutputValidationError(f"user did not permit stopping: {event_id}")
            if flexibility not in {"stoppable", "reducible_or_stoppable"}:
                raise OutputValidationError(f"event is not stoppable: {event_id}")
            continue

        if event.category not in user.expense_categories_user_is_willing_to_reduce:
            raise OutputValidationError(f"user did not permit reducing: {event_id}")
        if flexibility not in {"reducible", "reducible_or_stoppable"}:
            raise OutputValidationError(f"event is not reducible: {event_id}")
        try:
            new_amount = Decimal(reduce_match.group(2))
        except InvalidOperation as exc:
            raise OutputValidationError(f"invalid reduction amount: {part!r}") from exc
        if not new_amount.is_finite() or new_amount < 0 or new_amount > rule.amount:
            raise OutputValidationError(f"reduction amount is outside the recurring amount: {part!r}")


def validate_output_row(
    row: OutputRow,
    request: Request,
    user: User,
    options: Iterable[PaymentOption],
    events: Iterable[FinancialEvent],
    forecast: Forecast,
    converter: CurrencyConverter,
) -> None:
    """Raise OutputValidationError unless one row satisfies every output invariant."""
    if row.request_id != request.request_id:
        raise OutputValidationError("request_id does not match the input request")
    if row.affordability_status not in ALLOWED_STATUSES:
        raise OutputValidationError("invalid affordability_status")
    if row.recommended_payment_method not in ALLOWED_METHODS:
        raise OutputValidationError("invalid recommended_payment_method")
    if not row.amount_safe_to_pay.is_finite() or not (
        Decimal("0") <= row.amount_safe_to_pay <= request.requested_amount
    ):
        raise OutputValidationError("amount_safe_to_pay is outside the allowed range")
    if row.earliest_date_for_full_payment is not None and not (
        request.request_date <= row.earliest_date_for_full_payment <= forecast.end_date
    ):
        raise OutputValidationError("earliest_date_for_full_payment is outside the forecast")
    if not row.decision_explanation.strip():
        raise OutputValidationError("decision_explanation cannot be blank")

    options = tuple(options)
    events = tuple(events)
    _validate_spending_changes(
        row.spending_changes_needed,
        user=user,
        events=events,
        forecast=forecast,
        converter=converter,
    )
    payments = _parse_plan(row.payment_plan)
    methods = set(user.payment_methods_user_will_consider)

    if row.recommended_payment_method == "not_recommended":
        if row.affordability_status != "not_affordable":
            raise OutputValidationError("not_recommended requires not_affordable")
        if payments or row.spending_changes_needed != "none":
            raise OutputValidationError("not_recommended must have no payment plan or changes")
        return

    if row.payment_plan == "none":
        raise OutputValidationError("an actionable method requires a payment plan")
    if row.recommended_payment_method in {"full_payment", "partial_payment", "installments"}:
        if row.recommended_payment_method not in methods:
            raise OutputValidationError("recommended method is not user-accepted")
    if row.recommended_payment_method == "wait" and "full_payment" not in methods:
        raise OutputValidationError("wait requires user acceptance of full payment")

    if row.affordability_status == "affordable_now":
        if row.recommended_payment_method != "full_payment":
            raise OutputValidationError("affordable_now requires full_payment")
        if row.earliest_date_for_full_payment != request.request_date:
            raise OutputValidationError("affordable_now requires earliest date equal to request date")
    elif row.affordability_status == "affordable_later":
        if row.recommended_payment_method != "wait":
            raise OutputValidationError("affordable_later requires wait")
    elif row.affordability_status == "not_affordable":
        raise OutputValidationError("not_affordable requires not_recommended")

    total = sum((amount for _, amount in payments), Decimal("0"))
    if row.recommended_payment_method == "partial_payment":
        if row.affordability_status != "affordable_with_plan":
            raise OutputValidationError("partial_payment requires affordable_with_plan")
        if not request.allows_partial_payment or "partial_payment" not in methods:
            raise OutputValidationError("partial_payment is not eligible")
        if not (Decimal("0") < row.amount_safe_to_pay < request.requested_amount):
            raise OutputValidationError("partial_payment requires a non-trivial safe first payment")
        if len(payments) != 2:
            raise OutputValidationError("partial_payment requires exactly two payments")
        if payments[0] != (request.request_date, row.amount_safe_to_pay):
            raise OutputValidationError("partial_payment first payment is not the safe amount on request date")
        if row.earliest_date_for_full_payment is None:
            raise OutputValidationError("partial_payment requires an earliest full-payment date")
        if payments[1][0] != row.earliest_date_for_full_payment:
            raise OutputValidationError("partial_payment second date does not match earliest full date")
        if payments[1][1] != request.requested_amount - row.amount_safe_to_pay:
            raise OutputValidationError("partial_payment remainder is incorrect")
        if payments[1][0] > request.desired_completion_date or total != request.requested_amount:
            raise OutputValidationError("partial_payment misses deadline or does not sum to request")
    elif row.recommended_payment_method == "installments":
        if row.affordability_status != "affordable_with_plan":
            raise OutputValidationError("installments requires affordable_with_plan")
        matching = []
        for option in options:
            if option.payment_method != "installments":
                continue
            if option.number_of_payments > (user.max_installment_months or 0):
                continue
            schedule = _option_schedule(option)
            if payments == schedule and total == option.total_payable_amount:
                matching.append(option)
        if not matching:
            raise OutputValidationError("installment plan does not exactly match a supplied eligible option")
    elif row.recommended_payment_method == "full_payment":
        if total != request.requested_amount or payments != ((request.request_date, request.requested_amount),):
            raise OutputValidationError("full_payment must pay the full request on request date")
    elif row.recommended_payment_method == "wait":
        if row.affordability_status != "affordable_later":
            raise OutputValidationError("wait requires affordable_later")
        if row.earliest_date_for_full_payment is None:
            raise OutputValidationError("wait requires an earliest full-payment date")
        if payments != ((row.earliest_date_for_full_payment, request.requested_amount),):
            raise OutputValidationError("wait plan must pay full amount on earliest full date")
        if payments[0][0] > request.desired_completion_date:
            raise OutputValidationError("wait plan misses desired completion date")


def assemble_request(
    request: Request,
    user: User,
    options: Iterable[PaymentOption],
    events: Iterable[FinancialEvent],
    forecast: Forecast,
    converter: CurrencyConverter,
    explanation_generator: ExplanationGenerator,
) -> OutputRow:
    """Finalize numeric decision, then generate explanation, then validate."""
    decision = construct_plan(user, request, options, events, forecast, converter)
    explanation = explanation_generator.generate(request, decision)
    row = OutputRow(
        request_id=request.request_id,
        amount_safe_to_pay=decision.amount_safe_to_pay,
        affordability_status=decision.affordability_status,
        recommended_payment_method=decision.recommended_payment_method,
        payment_plan=decision.payment_plan,
        earliest_date_for_full_payment=decision.earliest_date_for_full_payment,
        spending_changes_needed=decision.spending_changes_needed,
        decision_explanation=explanation,
    )
    validate_output_row(row, request, user, options, events, forecast, converter)
    return row


def write_output_csv(rows: Iterable[OutputRow], path: Path | str) -> None:
    """Write only rows already validated by validate_output_row."""
    materialized = tuple(rows)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in materialized:
            writer.writerow(row.csv_values())


def append_hard_error(path: Path | str, request_id: str, error: BaseException) -> None:
    """Append a non-secret hard error; callers must omit the failed row."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"request_id": request_id, "error_type": type(error).__name__, "error": str(error)},
                sort_keys=True,
            )
            + "\n"
        )


def assemble_dataset(
    requests: Iterable[Request],
    *,
    users: Mapping[str, User],
    options_by_request_id: Mapping[str, Sequence[PaymentOption]],
    events_by_user_id: Mapping[str, Sequence[FinancialEvent]],
    forecasts_by_request_id: Mapping[str, Forecast],
    converter: CurrencyConverter,
    explanation_generator: ExplanationGenerator,
    hard_error_path: Path | str = Path("evaluation/phase5_errors.jsonl"),
) -> tuple[tuple[OutputRow, ...], tuple[tuple[str, str], ...]]:
    """Assemble valid rows and omit/log every missing or invalid row."""
    rows: list[OutputRow] = []
    errors: list[tuple[str, str]] = []
    for request in requests:
        try:
            row = assemble_request(
                request,
                users[request.user_id],
                options_by_request_id.get(request.request_id, ()),
                events_by_user_id[request.user_id],
                forecasts_by_request_id[request.request_id],
                converter,
                explanation_generator,
            )
        except Exception as exc:
            append_hard_error(hard_error_path, request.request_id, exc)
            errors.append((request.request_id, str(exc)))
            continue
        rows.append(row)
    return tuple(rows), tuple(errors)
