"""A tiny typed CSV loader and indexing layer used instead of pandas.

This module intentionally owns all CSV coercion and join indexes. Callers should
never parse dates, money, booleans, or pipe-separated fields themselves.
"""

from __future__ import annotations

import csv
import types
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Generic, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar, Union, get_args, get_origin, get_type_hints

from .models import (
    ExchangeRate,
    FinancialEvent,
    Image,
    Message,
    OutputTemplateRow,
    PaymentOption,
    Request,
    SolvedRequest,
    User,
)


T = TypeVar("T")


class CsvCoercionError(ValueError):
    """Raised when a non-blank CSV value cannot be converted to its field type."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_date(value: str) -> date:
    """Parse an ISO date or ISO timestamp into a datetime.date."""
    text = _text(value)
    if not text:
        raise ValueError("date cannot be blank")
    # sent_at is an ISO timestamp while all financial dates are ISO dates.
    iso_date = text.split("T", 1)[0].split(" ", 1)[0]
    try:
        return date.fromisoformat(iso_date)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def parse_decimal(value: str) -> Decimal:
    """Parse a finite base-10 number without passing through binary float."""
    text = _text(value)
    if not text:
        raise ValueError("decimal cannot be blank")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"decimal must be finite: {value!r}")
    return result


def parse_currency_amount(value: str) -> Decimal:
    """Parse a currency amount; kept explicit for money-bearing fields."""
    return parse_decimal(value)


def parse_bool(value: str) -> bool:
    """Parse the small, explicit boolean vocabulary used by the CSV files."""
    text = _text(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def parse_optional(value: Any, parser: Callable[[str], T]) -> Optional[T]:
    """Return None for a blank cell, otherwise apply one shared parser."""
    text = _text(value)
    return None if text == "" else parser(text)


def parse_pipe_list(value: str) -> Tuple[str, ...]:
    """Parse a pipe-separated preference list, preserving deterministic order."""
    text = _text(value)
    if not text:
        return ()
    return tuple(part.strip() for part in text.split("|") if part.strip())


def _is_union(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, types.UnionType)


def _coerce(raw: Any, annotation: Any, field_name: str) -> Any:
    """Coerce one value from a resolved dataclass annotation."""
    if annotation is Any:
        return raw

    if _is_union(annotation):
        args = get_args(annotation)
        non_none = tuple(arg for arg in args if arg is not type(None))
        if len(non_none) == 1 and len(non_none) != len(args):
            return parse_optional(raw, lambda text: _coerce(text, non_none[0], field_name))
        raise TypeError(f"unsupported union annotation for {field_name}: {annotation!r}")

    origin = get_origin(annotation)
    if origin in (tuple, Tuple, list, List):
        args = get_args(annotation)
        item_type = args[0] if args else str
        if item_type is not str:
            raise TypeError(f"only string collections are supported: {field_name}")
        parsed = parse_pipe_list(_text(raw))
        return parsed if origin in (tuple, Tuple) else list(parsed)

    text = _text(raw)
    try:
        if annotation is str:
            return text
        if annotation is date:
            return parse_date(text)
        if annotation is Decimal:
            return parse_currency_amount(text)
        if annotation is bool:
            return parse_bool(text)
        if annotation is int:
            if not text:
                raise ValueError("integer cannot be blank")
            return int(text)
    except (TypeError, ValueError) as exc:
        raise CsvCoercionError(f"field {field_name!r}: {exc}") from exc

    raise TypeError(f"unsupported annotation for {field_name}: {annotation!r}")


def load_csv(path: Path | str, row_type: Type[T]) -> List[T]:
    """Load every CSV row into a typed dataclass instance."""
    path = Path(path)
    type_hints = get_type_hints(row_type)
    field_defs = fields(row_type)
    expected = [field.name for field in field_defs]
    rows: List[T] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        actual = reader.fieldnames or []
        missing = [name for name in expected if name not in actual]
        if missing:
            raise ValueError(f"{path}: missing CSV columns: {missing}")
        for line_number, raw in enumerate(reader, start=2):
            try:
                values = {
                    name: _coerce(raw.get(name, ""), type_hints[name], name)
                    for name in expected
                }
                rows.append(row_type(**values))
            except (CsvCoercionError, TypeError, ValueError) as exc:
                raise CsvCoercionError(f"{path}:{line_number}: {exc}") from exc
    return rows


def index_by(rows: Iterable[T], attribute: str) -> Dict[Any, T]:
    """Create a unique-key index and reject duplicate identifiers."""
    result: Dict[Any, T] = {}
    for row in rows:
        key = getattr(row, attribute)
        if key in result:
            raise ValueError(f"duplicate {attribute}={key!r}")
        result[key] = row
    return result


def group_by(rows: Iterable[T], attribute: str) -> Dict[Any, List[T]]:
    """Create a one-to-many index for joins such as events by user_id."""
    result: Dict[Any, List[T]] = defaultdict(list)
    for row in rows:
        result[getattr(row, attribute)].append(row)
    return dict(result)


@dataclass(frozen=True)
class Dataset:
    """Typed dataset plus all indexes needed for O(1) joins."""

    users: List[User]
    events: List[FinancialEvent]
    requests: List[Request]
    sample_requests: List[SolvedRequest]
    payment_options: List[PaymentOption]
    messages: List[Message]
    images: List[Image]
    exchange_rates: List[ExchangeRate]
    output_template: List[OutputTemplateRow]
    user_by_id: Dict[str, User]
    event_by_id: Dict[str, FinancialEvent]
    request_by_id: Dict[str, Request]
    options_by_request_id: Dict[str, List[PaymentOption]]
    events_by_user_id: Dict[str, List[FinancialEvent]]
    messages_by_user_id: Dict[str, List[Message]]
    messages_by_event_id: Dict[str, List[Message]]
    images_by_event_id: Dict[str, List[Image]]
    images_by_request_id: Dict[str, List[Image]]


def load_dataset(dataset_dir: Path | str) -> Dataset:
    """Load every participant-facing CSV and construct deterministic indexes."""
    root = Path(dataset_dir)
    users = load_csv(root / "financial_profiles.csv", User)
    events = load_csv(root / "financial_events.csv", FinancialEvent)
    requests = load_csv(root / "requests.csv", Request)
    sample_requests = load_csv(root / "sample_requests.csv", SolvedRequest)
    payment_options = load_csv(root / "request_payment_options.csv", PaymentOption)
    messages = load_csv(root / "messages.csv", Message)
    images = load_csv(root / "images.csv", Image)
    output_template = load_csv(root / "output.csv", OutputTemplateRow)
    exchange_rates = load_csv(root / "exchange_rates.csv", ExchangeRate)

    return Dataset(
        users=users,
        events=events,
        requests=requests,
        sample_requests=sample_requests,
        payment_options=payment_options,
        messages=messages,
        images=images,
        exchange_rates=exchange_rates,
        output_template=output_template,
        user_by_id=index_by(users, "user_id"),
        event_by_id=index_by(events, "event_id"),
        request_by_id=index_by(requests, "request_id"),
        options_by_request_id=group_by(payment_options, "request_id"),
        events_by_user_id=group_by(events, "user_id"),
        messages_by_user_id=group_by(messages, "user_id"),
        messages_by_event_id=group_by(
            (message for message in messages if message.related_event_id),
            "related_event_id",
        ),
        images_by_event_id=group_by(
            (image for image in images if image.related_event_id),
            "related_event_id",
        ),
        images_by_request_id=group_by(
            (image for image in images if image.request_id),
            "request_id",
        ),
    )
