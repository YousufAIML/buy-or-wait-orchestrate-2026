"""Validated resolved-facts input shared across extraction and the engine.

The engine consumes this persisted JSON contract, never the extraction cache or
provider objects directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .dataframe_lite import parse_currency_amount


@dataclass(frozen=True, slots=True)
class ResolvedOverrides:
    image_amounts_by_event_id: Mapping[str, Decimal]
    message_actions_by_id: Mapping[str, str]


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def load_resolved_overrides(path: Path | str) -> ResolvedOverrides:
    """Load and validate the extraction-to-engine JSON boundary."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        document = _object(json.load(handle), "overrides")
    if document.get("version") != 1:
        raise ValueError("resolved overrides version must be 1")

    image_amounts: dict[str, Decimal] = {}
    for event_id, raw in _object(document.get("image_amounts", {}), "image_amounts").items():
        entry = _object(raw, f"image_amounts[{event_id!r}]")
        amount = parse_currency_amount(str(entry.get("amount", "")))
        if amount <= 0:
            raise ValueError(f"image amount for {event_id!r} must be positive")
        image_amounts[str(event_id)] = amount

    allowed_actions = {"confirm", "amend_amount", "amend_date", "cancel", "none"}
    message_actions: dict[str, str] = {}
    for message_id, raw in _object(document.get("message_classifications", {}), "message_classifications").items():
        entry = _object(raw, f"message_classifications[{message_id!r}]")
        action = str(entry.get("action", ""))
        if action not in allowed_actions:
            raise ValueError(f"invalid action for {message_id!r}: {action!r}")
        message_actions[str(message_id)] = action
    return ResolvedOverrides(image_amounts, message_actions)
