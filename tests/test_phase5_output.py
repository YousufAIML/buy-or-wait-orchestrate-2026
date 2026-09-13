import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.engine.currency import CurrencyConverter
from src.engine.forecast import build_forecast
from src.engine.models import FinancialEvent, PaymentOption, Request, User
from src.engine.output import (
    GroqExplanationGenerator,
    MockExplanationGenerator,
    OutputRow,
    OutputValidationError,
    assemble_request,
    validate_output_row,
)


START = date(2026, 1, 1)


def user(*, methods=("full_payment",), max_months=None):
    return User(
        user_id="u1",
        home_currency="USD",
        current_available_balance=Decimal("1000"),
        minimum_balance_to_keep=Decimal("100"),
        financial_priorities=(),
        expense_categories_to_protect=(),
        expense_categories_user_is_willing_to_reduce=(),
        expense_categories_user_is_willing_to_stop=(),
        payment_methods_user_will_consider=tuple(methods),
        max_installment_months=max_months,
    )


def request(*, amount="200", partial=False):
    return Request(
        request_id="r1",
        user_id="u1",
        request_date=START,
        request_type="purchase",
        requested_amount=Decimal(amount),
        desired_completion_date=date(2026, 1, 31),
        allows_partial_payment=partial,
        request_text="test",
    )


def option(method="full_payment", amount="200", count=1, first=START, frequency=None):
    total = Decimal(amount) * count
    return PaymentOption(
        payment_option_id="option_1",
        request_id="r1",
        payment_method=method,
        payment_amount=Decimal(amount),
        number_of_payments=count,
        first_payment_date=first,
        payment_frequency_days=frequency,
        financing_fee=Decimal("0"),
        total_payable_amount=total,
    )


class Phase5OutputTests(unittest.TestCase):
    def setUp(self):
        self.converter = CurrencyConverter([])
        self.user = user()
        self.req = request()
        self.events = []
        self.forecast = build_forecast(self.user, self.events, self.converter, START)

    def test_mock_assembly_generates_then_validates_finalized_row(self):
        row = assemble_request(
            self.req,
            self.user,
            [option()],
            self.events,
            self.forecast,
            self.converter,
            MockExplanationGenerator(),
        )
        self.assertEqual(row.affordability_status, "affordable_now")
        self.assertEqual(row.recommended_payment_method, "full_payment")
        self.assertEqual(row.payment_plan, "2026-01-01:200")
        self.assertIn("finalized payment plan", row.decision_explanation)

    def test_partial_payment_requires_exact_two_payment_schedule(self):
        row = OutputRow(
            request_id="r1",
            amount_safe_to_pay=Decimal("100"),
            affordability_status="affordable_with_plan",
            recommended_payment_method="partial_payment",
            payment_plan="2026-01-01:100|2026-01-05:99",
            earliest_date_for_full_payment=date(2026, 1, 5),
            spending_changes_needed="none",
            decision_explanation="mock",
        )
        with self.assertRaises(OutputValidationError):
            validate_output_row(
                row,
                request(amount="200", partial=True),
                user(methods=("partial_payment",)),
                [],
                self.events,
                self.forecast,
                self.converter,
            )

    def test_installments_must_match_supplied_option_exactly(self):
        installment_user = user(methods=("installments",), max_months=3)
        installment_request = request(amount="200")
        installment_option = option("installments", amount="100", count=2, frequency=10)
        row = OutputRow(
            request_id="r1",
            amount_safe_to_pay=Decimal("200"),
            affordability_status="affordable_with_plan",
            recommended_payment_method="installments",
            payment_plan="2026-01-01:100|2026-01-11:99",
            earliest_date_for_full_payment=START,
            spending_changes_needed="none",
            decision_explanation="mock",
        )
        installment_forecast = build_forecast(
            installment_user, self.events, self.converter, START
        )
        with self.assertRaises(OutputValidationError):
            validate_output_row(
                row,
                installment_request,
                installment_user,
                [installment_option],
                self.events,
                installment_forecast,
                self.converter,
            )

    def test_status_method_consistency_is_strict(self):
        row = OutputRow(
            request_id="r1",
            amount_safe_to_pay=Decimal("200"),
            affordability_status="affordable_now",
            recommended_payment_method="wait",
            payment_plan="2026-01-01:200",
            earliest_date_for_full_payment=START,
            spending_changes_needed="none",
            decision_explanation="mock",
        )
        with self.assertRaises(OutputValidationError):
            validate_output_row(
                row,
                self.req,
                self.user,
                [option()],
                self.events,
                self.forecast,
                self.converter,
            )

    def test_groq_prompt_contains_final_values_and_no_decision_instruction(self):
        captured = {}

        class FakeGroq:
            def chat_completion(self, messages, *, request_key):
                captured["messages"] = messages
                captured["request_key"] = request_key
                return SimpleNamespace(text="Finalized explanation.")

        decision = SimpleNamespace(
            amount_safe_to_pay=Decimal("20"),
            affordability_status="affordable_later",
            recommended_payment_method="wait",
            payment_plan="2026-01-10:200",
            earliest_date_for_full_payment=date(2026, 1, 10),
            spending_changes_needed="none",
        )
        result = GroqExplanationGenerator(FakeGroq()).generate(self.req, decision)
        joined = "\n".join(message["content"] for message in captured["messages"])
        self.assertEqual(result, "Finalized explanation.")
        self.assertEqual(captured["request_key"], "r1")
        self.assertIn('"amount_safe_to_pay": "20"', joined)
        self.assertIn('"recommended_payment_method": "wait"', joined)
        self.assertIn("Do not decide, rank, recalculate", joined)


if __name__ == "__main__":
    unittest.main()
