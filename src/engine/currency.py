"""Exact dated currency conversion using Decimal rates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, Tuple

from .models import ExchangeRate


class MissingExchangeRate(KeyError):
    """Raised when the dataset has no exact dated pair for a conversion."""


@dataclass(frozen=True)
class CurrencyConverter:
    """Convert only with an explicitly supplied rate-date and currency pair."""

    rates: Tuple[ExchangeRate, ...]

    def __init__(self, rates: Iterable[ExchangeRate]):
        materialized = tuple(rates)
        index: Dict[tuple[date, str, str], Decimal] = {}
        for rate in materialized:
            key = (rate.rate_date, rate.from_currency, rate.to_currency)
            if key in index:
                raise ValueError(f"duplicate exchange rate for {key!r}")
            index[key] = rate.rate
        object.__setattr__(self, "rates", materialized)
        object.__setattr__(self, "_index", index)

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        rate_date: date,
    ) -> Decimal:
        if not isinstance(amount, Decimal):
            raise TypeError("amount must be Decimal")
        if from_currency == to_currency:
            return amount
        key = (rate_date, from_currency, to_currency)
        try:
            rate = self._index[key]
        except KeyError as exc:
            raise MissingExchangeRate(
                f"no exchange rate for {from_currency}->{to_currency} on {rate_date.isoformat()}"
            ) from exc
        return amount * rate
