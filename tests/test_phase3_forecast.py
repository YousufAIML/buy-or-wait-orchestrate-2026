import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.engine.currency import CurrencyConverter
from src.engine.forecast import build_forecast
from src.engine.models import FinancialEvent, User
from src.engine.overrides import load_resolved_overrides


START = date(2026, 1, 1)


def user(balance="100", minimum="20"):
    return User(
        user_id="u1",
        home_currency="USD",
        current_available_balance=Decimal(balance),
        minimum_balance_to_keep=Decimal(minimum),
        financial_priorities=(),
        expense_categories_to_protect=("rent", "groceries"),
        expense_categories_user_is_willing_to_reduce=("dining",),
        expense_categories_user_is_willing_to_stop=("streaming",),
        payment_methods_user_will_consider=("full_payment",),
        max_installment_months=None,
    )


def event(
    event_id,
    event_date,
    *,
    amount="10",
    direction="debit",
    category="expense",
    event_type="expense",
    status="settled",
    settlement_date=None,
    description="Synthetic event",
    flexibility="fixed",
):
    return FinancialEvent(
        event_id=event_id,
        user_id="u1",
        event_type=event_type,
        description=description,
        category=category,
        direction=direction,
        amount=Decimal(amount) if amount is not None else None,
        currency="USD",
        event_date=event_date,
        settlement_date=settlement_date if settlement_date is not None else event_date,
        status=status,
        linked_event_id=None,
        flexibility=flexibility,
        minimum_allowed_amount=None,
    )


class ForecastSafetyTests(unittest.TestCase):
    def setUp(self):
        self.converter = CurrencyConverter([])

    def test_minimum_is_never_at_risk(self):
        forecast = build_forecast(user(), [], self.converter, START)
        self.assertEqual(forecast.amount_safe_to_pay(Decimal("50")), Decimal("50"))
        self.assertEqual(forecast.earliest_date_for_full_payment(Decimal("50")), START)
        self.assertTrue(forecast.is_payment_safe(START, Decimal("80")))

    def test_already_below_minimum_has_zero_safe_today_and_no_full_date(self):
        forecast = build_forecast(user(balance="10", minimum="20"), [], self.converter, START)
        self.assertEqual(forecast.amount_safe_to_pay(Decimal("1")), Decimal("0"))
        self.assertIsNone(forecast.earliest_date_for_full_payment(Decimal("1")))
        self.assertFalse(forecast.is_payment_safe(START, Decimal("0")))

    def test_payment_can_look_safe_today_but_break_on_a_later_day(self):
        events = [
            event("debit_day_2", START + timedelta(days=2), amount="70"),
            event(
                "credit_day_3",
                START + timedelta(days=3),
                amount="100",
                direction="credit",
                category="salary",
                event_type="income",
            ),
        ]
        forecast = build_forecast(user(), events, self.converter, START)
        self.assertEqual(forecast.balances_without_request[START + timedelta(days=2)], Decimal("30"))
        self.assertEqual(forecast.amount_safe_to_pay(Decimal("50")), Decimal("10"))
        self.assertFalse(forecast.is_payment_safe(START, Decimal("11")))
        self.assertEqual(
            forecast.earliest_date_for_full_payment(Decimal("50")),
            START + timedelta(days=3),
        )

    def test_no_recurring_events_still_has_a_finite_cash_capacity(self):
        forecast = build_forecast(user(), [], self.converter, START)
        self.assertEqual(forecast.recurring_rules, ())
        self.assertEqual(forecast.amount_safe_to_pay(Decimal("100")), Decimal("80"))
        self.assertIsNone(forecast.earliest_date_for_full_payment(Decimal("100")))
        self.assertEqual(forecast.balances_without_request[forecast.end_date], Decimal("100"))

    def test_regular_history_projects_monthly_event_and_uses_day_by_day_balances(self):
        history = [
            event("rent_1", date(2025, 12, 1), amount="30", category="rent"),
            event("rent_2", date(2026, 1, 1), amount="30", category="rent"),
            event("rent_3", date(2026, 2, 1), amount="30", category="rent"),
        ]
        request_date = date(2026, 2, 2)
        forecast = build_forecast(user(balance="100", minimum="20"), history, self.converter, request_date)
        projected_dates = [
            item.event_date
            for items in forecast.events_by_date.values()
            for item in items
            if item.recurring
        ]
        self.assertEqual(projected_dates, [date(2026, 3, 4), date(2026, 4, 4)])
        self.assertEqual(forecast.balances_without_request[date(2026, 3, 4)], Decimal("70"))

    def test_regular_monthly_salary_with_irregular_bonus_projects_next_salary(self):
        history = [
            event(
                "salary_october",
                date(2025, 10, 15),
                amount="50",
                direction="credit",
                category="salary",
                event_type="income",
                description="Confirmed monthly salary",
            ),
            event(
                "salary_november",
                date(2025, 11, 15),
                amount="50",
                direction="credit",
                category="salary",
                event_type="income",
                description="Confirmed monthly salary",
            ),
            event(
                "salary_december",
                date(2025, 12, 15),
                amount="50",
                direction="credit",
                category="salary",
                event_type="income",
                description="Confirmed monthly salary",
            ),
            event(
                "irregular_bonus",
                date(2025, 12, 22),
                amount="125",
                direction="credit",
                category="salary",
                event_type="income",
                description="One-off performance bonus",
            ),
        ]
        forecast = build_forecast(
            user(balance="100", minimum="20"),
            history,
            self.converter,
            date(2026, 1, 2),
        )
        salary_rules = [
            rule
            for rule in forecast.recurring_rules
            if rule.category == "salary" and rule.direction == "credit"
        ]
        self.assertEqual(len(salary_rules), 1)
        self.assertEqual(salary_rules[0].source_event_id, "salary_december")
        self.assertEqual(salary_rules[0].amount, Decimal("50"))
        projected_salary = [
            item
            for items in forecast.events_by_date.values()
            for item in items
            if item.recurring and item.direction == "credit" and item.category == "salary"
        ]
        self.assertTrue(projected_salary)
        self.assertEqual(projected_salary[0].amount, Decimal("50"))
        self.assertEqual(projected_salary[0].event_date, date(2026, 1, 14))

    def test_dense_ten_day_groceries_prefer_full_group_cadence(self):
        history = [
            event(
                f"groceries_{index}",
                date(2025, 1, 10) + timedelta(days=10 * index),
                amount="25",
                category="groceries",
                flexibility="fixed",
            )
            for index in range(18)
        ]
        request_date = history[-1].event_date + timedelta(days=1)
        forecast = build_forecast(user(), history, self.converter, request_date)
        groceries_rules = [
            rule for rule in forecast.recurring_rules if rule.category == "groceries"
        ]
        self.assertEqual(len(groceries_rules), 1)
        self.assertEqual(groceries_rules[0].period_days, 10)
        projected = [
            item
            for items in forecast.events_by_date.values()
            for item in items
            if item.recurring and item.category == "groceries"
        ]
        self.assertEqual(len(projected), 9)

    def test_dense_five_day_transport_prefer_full_group_cadence(self):
        history = [
            event(
                f"transport_{index}",
                date(2025, 1, 2) + timedelta(days=5 * index),
                amount="18",
                category="transport",
                flexibility="fixed",
            )
            for index in range(36)
        ]
        request_date = history[-1].event_date + timedelta(days=1)
        forecast = build_forecast(user(), history, self.converter, request_date)
        transport_rules = [
            rule for rule in forecast.recurring_rules if rule.category == "transport"
        ]
        self.assertEqual(len(transport_rules), 1)
        self.assertEqual(transport_rules[0].period_days, 5)
        projected = [
            item
            for items in forecast.events_by_date.values()
            for item in items
            if item.recurring and item.category == "transport"
        ]
        self.assertEqual(len(projected), 18)

    def test_dense_weekly_stream_wins_over_same_day_monthly_coincidences(self):
        history = [
            event(
                f"weekly_{index}",
                date(2025, 1, 3) + timedelta(days=7 * index),
                amount="12",
                category="entertainment",
                flexibility="fixed",
            )
            for index in range(15)
        ]
        request_date = history[-1].event_date + timedelta(days=1)
        forecast = build_forecast(user(), history, self.converter, request_date)
        entertainment_rules = [
            rule
            for rule in forecast.recurring_rules
            if rule.category == "entertainment"
        ]
        self.assertEqual(len(entertainment_rules), 1)
        self.assertEqual(entertainment_rules[0].period_days, 7)
        projected = [
            item
            for items in forecast.events_by_date.values()
            for item in items
            if item.recurring and item.category == "entertainment"
        ]
        self.assertEqual(len(projected), 13)

    def test_future_pending_debit_uses_settlement_date_and_confirmed_salary_is_counted(self):
        events = [
            event(
                "pending_debit",
                START - timedelta(days=1),
                amount="25",
                status="pending",
                settlement_date=START + timedelta(days=2),
                description="Pending debit",
            ),
            event(
                "next_salary",
                START - timedelta(days=1),
                amount="50",
                direction="credit",
                category="salary",
                event_type="income",
                status="scheduled",
                settlement_date=START + timedelta(days=3),
                description="Next confirmed salary",
            ),
        ]
        forecast = build_forecast(user(), events, self.converter, START)
        self.assertEqual(forecast.balances_without_request[START + timedelta(days=2)], Decimal("75"))
        self.assertEqual(forecast.balances_without_request[START + timedelta(days=3)], Decimal("125"))


class OverrideBoundaryTests(unittest.TestCase):
    def test_persisted_overrides_are_typed_and_validated(self):
        document = {
            "version": 1,
            "image_amounts": {"event_1": {"amount": "12.50", "currency": "USD"}},
            "message_classifications": {"message_1": {"action": "confirm"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resolved_overrides.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            overrides = load_resolved_overrides(path)
        self.assertEqual(overrides.image_amounts_by_event_id["event_1"], Decimal("12.50"))
        self.assertEqual(overrides.message_actions_by_id["message_1"], "confirm")


if __name__ == "__main__":
    unittest.main()
