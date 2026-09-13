"""Phase 0 reconnaissance executed through the typed CSV layer."""

from __future__ import annotations

import sys
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Type, get_type_hints

from .dataframe_lite import Dataset, load_dataset
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


def _null_count(rows: Iterable[Any], field_name: str) -> int:
    return sum(getattr(row, field_name) is None for row in rows)


def _field_types(row_type: Type[Any]) -> dict[str, str]:
    return {name: repr(annotation) for name, annotation in get_type_hints(row_type).items()}


def _date_range(rows: Iterable[Any], field_name: str) -> tuple[date, date]:
    values = [getattr(row, field_name) for row in rows if getattr(row, field_name) is not None]
    return min(values), max(values)


def print_recon(root: Path, dataset: Dataset) -> None:
    tables = [
        ("exchange_rates.csv", dataset.exchange_rates, ExchangeRate),
        ("financial_events.csv", dataset.events, FinancialEvent),
        ("financial_profiles.csv", dataset.users, User),
        ("images.csv", dataset.images, Image),
        ("messages.csv", dataset.messages, Message),
        ("output.csv", dataset.output_template, OutputTemplateRow),
        ("request_payment_options.csv", dataset.payment_options, PaymentOption),
        ("requests.csv", dataset.requests, Request),
        ("sample_requests.csv", dataset.sample_requests, SolvedRequest),
    ]
    for filename, rows, row_type in tables:
        names = [field.name for field in fields(row_type)]
        nulls = {name: _null_count(rows, name) for name in names}
        nulls = {name: count for name, count in nulls.items() if count}
        print(f"=== dataset/{filename} (typed {row_type.__name__}) ===")
        print(f"rows={len(rows)} columns={len(names)}")
        print(f"columns={names!r}")
        print(f"types={_field_types(row_type)!r}")
        print(f"null_counts={nulls!r}")
        print("sample3=")
        for row in rows[:3]:
            print(repr(row))
        print()

    image_dir = root / "media" / "images"
    blank_amount_events = [event for event in dataset.events if event.amount is None]
    print("=== typed consistency checks ===")
    print("request_ids_unique=", len(dataset.requests) == len(dataset.request_by_id))
    print("profile_users_unique=", len(dataset.users) == len(dataset.user_by_id))
    print("request_date_range=", _date_range(dataset.requests, "request_date"))
    print("event_date_range=", _date_range(dataset.events, "event_date"))
    print("rate_date_range=", _date_range(dataset.exchange_rates, "rate_date"))
    print("sample_rows=", len(dataset.sample_requests))
    print(
        "blank_event_amounts=",
        len(blank_amount_events),
        "blank_amounts_with_image_link=",
        sum(event.event_id in dataset.images_by_event_id for event in blank_amount_events),
    )
    print(
        "image_files_present=",
        sum((image_dir / f"{image.image_id}.png").exists() for image in dataset.images),
        "/",
        len(dataset.images),
    )
    print("event_linked_rows=", sum(event.linked_event_id is not None for event in dataset.events))
    print("messages_with_event_links=", sum(message.related_event_id is not None for message in dataset.messages))
    print("messages_with_request_links=", sum(message.request_id is not None for message in dataset.messages))
    print("options_per_request_range=", (min(map(len, dataset.options_by_request_id.values())), max(map(len, dataset.options_by_request_id.values()))))


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dataset")
    print_recon(root, load_dataset(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
