import unittest
from datetime import date, timedelta
from decimal import Decimal

from src.engine.currency import CurrencyConverter
from src.engine.forecast import build_forecast
from src.engine.models import FinancialEvent, PaymentOption, Request, User
from src.engine.plans import (
    PlanCandidate,
    PlanPayment,
    SpendingChange,
    construct_plan,
    rank_plan_candidates,
)


START = date(2026, 1, 1)


def phase_user(
    *,
    balance="100",
    minimum="20",
    methods=("full_payment",),
    stop=(),
    reduce=(),
):
    return User(
        user_id="u1",
        home_currency="USD",
        current_available_balance=Decimal(balance),
        minimum_balance_to_keep=Decimal(minimum),
        financial_priorities=(),
        expense_categories_to_protect=(),
        expense_categories_user_is_willing_to_reduce=tuple(reduce),
        expense_categories_user_is_willing_to_stop=tuple(stop),
        payment_methods_user_will_consider=tuple(methods),
        max_installment_months=None,
    )


def phase_event(
    event_id,
    event_date,
    *,
    amount="70",
    category="dining",
    flexibility="fixed",
):
    return FinancialEvent(
        event_id=event_id,
        user_id="u1",
        event_type="expense",
        description="Synthetic recurring expense",
        category=category,
        direction="debit",
        amount=Decimal(amount),
        currency="USD",
        event_date=event_date,
        settlement_date=event_date,
        status="settled",
        linked_event_id=None,
        flexibility=flexibility,
        minimum_allowed_amount=None,
    )


def request(*, amount="80", partial=False, desired=START + timedelta(days=30)):
    return Request(
        request_id="r1",
        user_id="u1",
        request_date=START,
        request_type="purchase",
        requested_amount=Decimal(amount),
        desired_completion_date=desired,
        allows_partial_payment=partial,
        request_text="Synthetic request",
    )


def full_option(amount="80"):
    return PaymentOption(
        payment_option_id="payment_option_1",
        request_id="r1",
        payment_method="full_payment",
        payment_amount=Decimal(amount),
        number_of_payments=1,
        first_payment_date=START,
        payment_frequency_days=None,
        financing_fee=Decimal("0"),
        total_payable_amount=Decimal(amount),
    )


class PlanRankingTests(unittest.TestCase):
    def payment_candidate(
        self,
        *,
        option_id,
        total,
        start,
        count=1,
        completes=True,
        changes=(),
    ):
        return PlanCandidate(
            method="installments",
            payments=tuple(
                PlanPayment(start + timedelta(days=index), Decimal(total) / count)
                for index in range(count)
            ),
            total_paid=Decimal(total),
            payment_option_id=option_id,
            spending_changes=tuple(changes),
            completes_by_deadline=completes,
        )

    def test_ranking_applies_deadline_changes_cost_start_count_and_id(self):
        candidates = [
            self.payment_candidate(
                option_id="late-cheap",
                total="1",
                start=date(2026, 1, 1),
                completes=False,
            ),
            self.payment_candidate(
                option_id="deadline-with-change",
                total="1",
                start=date(2026, 1, 1),
                changes=(SpendingChange("stop", "event_change"),),
            ),
            self.payment_candidate(
                option_id="expensive",
                total="20",
                start=date(2026, 1, 3),
                count=3,
            ),
            self.payment_candidate(
                option_id="cheap-late",
                total="10",
                start=date(2026, 1, 3),
            ),
            self.payment_candidate(
                option_id="cheap-later-start",
                total="10",
                start=date(2026, 1, 2),
            ),
            self.payment_candidate(
                option_id="cheap-early-many",
                total="10",
                start=date(2026, 1, 1),
                count=3,
            ),
            self.payment_candidate(
                option_id="z-option",
                total="10",
                start=date(2026, 1, 1),
                count=2,
            ),
            self.payment_candidate(
                option_id="a-option",
                total="10",
                start=date(2026, 1, 1),
                count=2,
            ),
        ]
        ranked = rank_plan_candidates(candidates)
        self.assertEqual(ranked[0].payment_option_id, "a-option")
        self.assertEqual(
            [candidate.payment_option_id for candidate in ranked[:5]],
            ["a-option", "z-option", "cheap-early-many", "cheap-later-start", "cheap-late"],
        )
        self.assertEqual(ranked[-1].payment_option_id, "late-cheap")


class PlanConstructionTests(unittest.TestCase):
    def setUp(self):
        self.converter = CurrencyConverter([])

    def test_only_flexible_spending_change_makes_full_payment_safe(self):
        events = [
            phase_event("dining_1", date(2025, 12, 14), flexibility="stoppable"),
            phase_event("dining_2", date(2025, 12, 21), flexibility="stoppable"),
            phase_event("dining_3", date(2025, 12, 28), flexibility="stoppable"),
        ]
        user = phase_user(stop=("dining",))
        req = request()
        forecast = build_forecast(user, events, self.converter, START)
        decision = construct_plan(user, req, [full_option()], events, forecast, self.converter)
        self.assertEqual(decision.recommended_payment_method, "full_payment")
        self.assertEqual(decision.affordability_status, "affordable_with_plan")
        self.assertEqual(decision.spending_changes_needed, "stop:dining_3")
        self.assertTrue(decision.selected_candidate)
        self.assertTrue(decision.selected_candidate.safe)

    def test_nothing_safe_returns_not_recommended(self):
        events = [
            phase_event("fixed_1", date(2025, 12, 14)),
            phase_event("fixed_2", date(2025, 12, 21)),
            phase_event("fixed_3", date(2025, 12, 28)),
        ]
        user = phase_user()
        req = request()
        forecast = build_forecast(user, events, self.converter, START)
        decision = construct_plan(user, req, [full_option()], events, forecast, self.converter)
        self.assertEqual(decision.affordability_status, "not_affordable")
        self.assertEqual(decision.recommended_payment_method, "not_recommended")
        self.assertEqual(decision.payment_plan, "none")
        self.assertEqual(decision.spending_changes_needed, "none")
        self.assertIsNone(decision.selected_candidate)


if __name__ == "__main__":
    unittest.main()
