"""Typed rows for the Buy or Wait input and output CSV files."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: Tuple[str, ...]
    expense_categories_to_protect: Tuple[str, ...]
    expense_categories_user_is_willing_to_reduce: Tuple[str, ...]
    expense_categories_user_is_willing_to_stop: Tuple[str, ...]
    payment_methods_user_will_consider: Tuple[str, ...]
    max_installment_months: Optional[int]


@dataclass(frozen=True, slots=True)
class FinancialEvent:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[Decimal]
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str
    linked_event_id: Optional[str]
    flexibility: Optional[str]
    minimum_allowed_amount: Optional[Decimal]


@dataclass(frozen=True, slots=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: Decimal
    total_payable_amount: Decimal


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: date
    source_type: str
    message_text: str


@dataclass(frozen=True, slots=True)
class Image:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(frozen=True, slots=True)
class SolvedRequest:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str


@dataclass(frozen=True, slots=True)
class OutputTemplateRow:
    request_id: str
    amount_safe_to_pay: Optional[Decimal]
    affordability_status: Optional[str]
    recommended_payment_method: Optional[str]
    payment_plan: Optional[str]
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: Optional[str]
    decision_explanation: Optional[str]
