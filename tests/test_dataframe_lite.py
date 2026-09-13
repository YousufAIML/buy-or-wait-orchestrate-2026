import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.engine.dataframe_lite import (
    CsvCoercionError,
    group_by,
    index_by,
    load_csv,
    load_dataset,
    parse_bool,
    parse_currency_amount,
    parse_date,
    parse_decimal,
    parse_optional,
)
from src.engine.models import FinancialEvent, Request


class CoercionTests(unittest.TestCase):
    def test_decimal_never_round_trips_through_float(self):
        total = parse_decimal("0.1") + parse_currency_amount("0.2")
        self.assertEqual(total, Decimal("0.3"))
        self.assertIsInstance(total, Decimal)

    def test_date_parser_accepts_date_and_timestamp(self):
        self.assertEqual(parse_date("2026-09-04"), date(2026, 9, 4))
        self.assertEqual(parse_date("2026-09-04T09:30:00Z"), date(2026, 9, 4))
        self.assertIsInstance(parse_date("2026-09-04"), date)

    def test_boolean_parser_is_explicit(self):
        self.assertTrue(parse_bool("true"))
        self.assertFalse(parse_bool("FALSE"))
        with self.assertRaises(ValueError):
            parse_bool("maybe")

    def test_optional_parser_maps_only_blank_to_none(self):
        self.assertIsNone(parse_optional("", parse_decimal))
        self.assertEqual(parse_optional("12.50", parse_decimal), Decimal("12.50"))
        with self.assertRaises(ValueError):
            parse_optional("not-a-number", parse_decimal)


class TypedLoadingTests(unittest.TestCase):
    def test_request_csv_becomes_typed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.csv"
            path.write_text(
                "request_id,user_id,request_date,request_type,requested_amount,desired_completion_date,allows_partial_payment,request_text\n"
                "r1,u1,2026-09-04,purchase,0.1,2026-09-10,true,Question\n",
                encoding="utf-8",
            )
            rows = load_csv(path, Request)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].requested_amount, Decimal("0.1"))
        self.assertEqual(rows[0].request_date, date(2026, 9, 4))
        self.assertTrue(rows[0].allows_partial_payment)

    def test_blank_optional_event_values_are_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            fieldnames = [field.name for field in FinancialEvent.__dataclass_fields__.values()]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "event_id": "e1",
                        "user_id": "u1",
                        "event_type": "expense",
                        "description": "test",
                        "category": "dining",
                        "direction": "debit",
                        "amount": "",
                        "currency": "USD",
                        "event_date": "2026-09-04",
                        "settlement_date": "",
                        "status": "pending",
                        "linked_event_id": "",
                        "flexibility": "flexible",
                        "minimum_allowed_amount": "",
                    }
                )
            rows = load_csv(path, FinancialEvent)
        self.assertIsNone(rows[0].amount)
        self.assertIsNone(rows[0].settlement_date)
        self.assertIsNone(rows[0].minimum_allowed_amount)

    def test_missing_required_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("request_id\nr1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_csv(path, Request)

    def test_duplicate_and_group_indexes_are_deterministic(self):
        rows = [Request("r1", "u1", date(2026, 1, 1), "purchase", Decimal("1"), date(2026, 1, 2), False, ""),
                Request("r2", "u1", date(2026, 1, 1), "purchase", Decimal("2"), date(2026, 1, 2), False, "")]
        self.assertEqual(index_by(rows, "request_id")["r2"].requested_amount, Decimal("2"))
        self.assertEqual(len(group_by(rows, "user_id")["u1"]), 2)
        with self.assertRaises(ValueError):
            index_by(rows + [rows[0]], "request_id")

    def test_real_dataset_is_fully_typed_and_indexed(self):
        data = load_dataset(Path(__file__).parents[1] / "dataset")
        self.assertEqual(len(data.events), 25342)
        self.assertEqual(len(data.requests), 250)
        self.assertEqual(len(data.sample_requests), 25)
        self.assertIsInstance(data.events[0].amount, Decimal)
        self.assertIsInstance(data.exchange_rates[0].rate, Decimal)
        self.assertIsInstance(data.requests[0].request_date, date)
        self.assertIs(data.request_by_id["request_26"], data.requests[0])
        self.assertEqual(len(data.options_by_request_id["request_01"]), 4)
        self.assertEqual(len(data.images_by_event_id), 16)


if __name__ == "__main__":
    unittest.main()
