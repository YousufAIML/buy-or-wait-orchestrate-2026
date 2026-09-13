"""Phase 6 Step 5 final validator and output.csv writer."""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine.currency import CurrencyConverter
from src.engine.dataframe_lite import load_dataset
from src.engine.forecast import build_forecast
from src.engine.output import (
    OUTPUT_COLUMNS,
    OutputRow,
    check_explanation_consistency,
    normalize_explanation,
    validate_output_row,
    write_output_csv,
)
from src.engine.overrides import load_resolved_overrides
from src.engine.plans import construct_plan

LIVE_EXPLANATIONS = Path("evaluation/live_explanations.json")
OUTPUT_PATH = Path("output.csv")


def run() -> dict:
    data = load_dataset("dataset")
    live = json.loads(LIVE_EXPLANATIONS.read_text(encoding="utf-8"))
    explanations = live.get("explanations", {})
    if len(explanations) != len(data.requests):
        raise ValueError(f"expected {len(data.requests)} explanations, found {len(explanations)}")
    if live.get("failures"):
        raise ValueError(f"live explanation failures remain: {live['failures']}")
    if live.get("consistency_flags"):
        raise ValueError(f"live explanation consistency flags remain: {live['consistency_flags']}")

    converter = CurrencyConverter(data.exchange_rates)
    overrides = load_resolved_overrides("evaluation/resolved_overrides.json")
    rows: list[OutputRow] = []
    failures: list[tuple[str, str]] = []
    for request in data.requests:
        try:
            user = data.user_by_id[request.user_id]
            events = data.events_by_user_id[request.user_id]
            forecast = build_forecast(
                user,
                events,
                converter,
                request.request_date,
                resolved_overrides=overrides,
            )
            decision = construct_plan(
                user,
                request,
                data.options_by_request_id.get(request.request_id, ()),
                events,
                forecast,
                converter,
            )
            explanation = normalize_explanation(explanations[request.request_id].strip())
            consistency_flags = check_explanation_consistency(explanation, request, decision)
            if consistency_flags:
                raise ValueError("; ".join(consistency_flags))
            row = OutputRow(
                request_id=request.request_id,
                amount_safe_to_pay=decision.amount_safe_to_pay,
                affordability_status=decision.affordability_status,
                recommended_payment_method=decision.recommended_payment_method,
                payment_plan=decision.payment_plan,
                earliest_date_for_full_payment=decision.earliest_date_for_full_payment,
                spending_changes_needed=decision.spending_changes_needed,
                decision_explanation=explanation,
            )
            validate_output_row(
                row,
                request,
                user,
                data.options_by_request_id.get(request.request_id, ()),
                events,
                forecast,
                converter,
            )
            rows.append(row)
        except Exception as exc:
            failures.append((request.request_id, f"{type(exc).__name__}: {exc}"))

    if failures:
        raise ValueError(f"final validation failed for {len(failures)} rows: {failures}")
    if len(rows) != len(data.requests):
        raise ValueError("refusing to write incomplete output.csv")

    write_output_csv(rows, OUTPUT_PATH)
    with OUTPUT_PATH.open("r", newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
        fieldnames = tuple(csv.DictReader(OUTPUT_PATH.open("r", newline="", encoding="utf-8")).fieldnames or ())
    if fieldnames != OUTPUT_COLUMNS or len(written) != len(data.requests):
        raise ValueError("written output.csv failed final shape check")

    rng = random.Random(20260913)
    selected_ids = {request.request_id for request in rng.sample(data.requests, 5)}
    spot_checks = [row.csv_values() for row in rows if row.request_id in selected_ids]
    return {
        "validator_pass": len(rows),
        "validator_fail": 0,
        "output_path": str(OUTPUT_PATH),
        "output_rows": len(written),
        "columns": list(fieldnames),
        "spot_checks": spot_checks,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=False))
