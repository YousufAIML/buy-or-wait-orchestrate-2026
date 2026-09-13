import unittest
from datetime import date
from decimal import Decimal

from src.engine.currency import CurrencyConverter, MissingExchangeRate
from src.engine.models import ExchangeRate, FinancialEvent
from src.engine.resolver import resolve_event_lifecycles


def event(
    event_id,
    *,
    status="settled",
    direction="debit",
    amount="100",
    description="Card purchase",
    category="shopping",
    event_date=date(2026, 1, 1),
    linked_event_id=None,
):
    return FinancialEvent(
        event_id=event_id,
        user_id="u1",
        event_type="expense",
        description=description,
        category=category,
        direction=direction,
        amount=Decimal(amount) if amount is not None else None,
        currency="USD",
        event_date=event_date,
        settlement_date=event_date if status == "settled" else None,
        status=status,
        linked_event_id=linked_event_id,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )


class CurrencyTests(unittest.TestCase):
    def test_exact_dated_decimal_conversion(self):
        converter = CurrencyConverter(
            [ExchangeRate(date(2026, 1, 1), "USD", "EUR", Decimal("0.92"))]
        )
        self.assertEqual(
            converter.convert(Decimal("10.25"), "USD", "EUR", date(2026, 1, 1)),
            Decimal("9.4300"),
        )
        self.assertEqual(
            converter.convert(Decimal("10.25"), "USD", "USD", date(2099, 1, 1)),
            Decimal("10.25"),
        )

    def test_missing_rate_is_not_silently_inverted_or_defaulted(self):
        converter = CurrencyConverter(
            [ExchangeRate(date(2026, 1, 1), "USD", "EUR", Decimal("0.92"))]
        )
        with self.assertRaises(MissingExchangeRate):
            converter.convert(Decimal("1"), "EUR", "USD", date(2026, 1, 1))
        with self.assertRaises(MissingExchangeRate):
            converter.convert(Decimal("1"), "USD", "EUR", date(2026, 1, 2))


class ResolverTests(unittest.TestCase):
    def test_settled_purchase_replaces_cancelled_authorization(self):
        result = resolve_event_lifecycles(
            [
                event("auth", status="cancelled", description="Card authorization"),
                event(
                    "purchase",
                    description="Settled card purchase",
                    linked_event_id="auth",
                    event_date=date(2026, 1, 2),
                ),
            ]
        )
        by_id = result.by_event_id
        self.assertFalse(by_id["auth"].included_in_cash_forecast)
        self.assertTrue(by_id["purchase"].included_in_cash_forecast)
        self.assertEqual(by_id["auth"].canonical_event_id, "purchase")
        self.assertTrue(any("auth" in entry for entry in result.conflicts))

    def test_settled_original_wins_over_pending_duplicate(self):
        result = resolve_event_lifecycles(
            [
                event("original", description="Original card charge"),
                event(
                    "duplicate",
                    status="pending",
                    description="Possible duplicate card charge",
                    linked_event_id="original",
                    event_date=date(2026, 1, 2),
                ),
            ]
        )
        by_id = result.by_event_id
        self.assertTrue(by_id["original"].included_in_cash_forecast)
        self.assertFalse(by_id["duplicate"].included_in_cash_forecast)
        self.assertEqual(by_id["duplicate"].canonical_event_id, "original")

    def test_legitimate_refund_credit_is_not_collapsed_into_expense(self):
        result = resolve_event_lifecycles(
            [
                event("purchase", description="Purchase awaiting refund"),
                event(
                    "refund",
                    direction="credit",
                    description="Settled merchant refund",
                    event_date=date(2026, 1, 3),
                    linked_event_id="purchase",
                ),
            ]
        )
        by_id = result.by_event_id
        self.assertTrue(by_id["purchase"].included_in_cash_forecast)
        self.assertTrue(by_id["refund"].included_in_cash_forecast)

    def test_cash_state_rules_for_pending_credit_failed_and_unrealized(self):
        result = resolve_event_lifecycles(
            [
                event("pending_credit", status="pending", direction="credit"),
                event("scheduled_debit", status="scheduled"),
                event("failed", status="failed"),
                event("portfolio", status="unrealized", direction="non_cash"),
            ]
        )
        by_id = result.by_event_id
        self.assertFalse(by_id["pending_credit"].included_in_cash_forecast)
        self.assertTrue(by_id["scheduled_debit"].included_in_cash_forecast)
        self.assertFalse(by_id["failed"].included_in_cash_forecast)
        self.assertFalse(by_id["portfolio"].included_in_cash_forecast)

    def test_settled_record_supersedes_same_source_forecast(self):
        result = resolve_event_lifecycles(
            [
                event(
                    "forecast",
                    status="pending",
                    description="Employer forecast salary",
                    category="salary",
                    direction="credit",
                ),
                event(
                    "confirmed",
                    description="Employer settled salary",
                    category="salary",
                    direction="credit",
                    event_date=date(2026, 1, 2),
                    linked_event_id="forecast",
                ),
            ]
        )
        by_id = result.by_event_id
        self.assertFalse(by_id["forecast"].included_in_cash_forecast)
        self.assertTrue(by_id["confirmed"].included_in_cash_forecast)


if __name__ == "__main__":
    unittest.main()
