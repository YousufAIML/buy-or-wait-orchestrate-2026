"""Buy or Wait Phase 5 mock validation and output assembly entry point.

The default command performs no live model calls. Live Groq explanations are a
separate future step and must be deliberately enabled by a later phase.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine.currency import CurrencyConverter
from src.engine.dataframe_lite import Dataset, load_dataset
from src.engine.forecast import MissingForecastAmount, build_forecast
from src.engine.models import Request
from src.engine.output import MockExplanationGenerator, assemble_dataset, write_output_csv
from src.engine.overrides import load_resolved_overrides


def _forecastable_requests(
    requests: Iterable[Request],
    data: Dataset,
    converter: CurrencyConverter,
    overrides: object,
) -> tuple[list[Request], dict[str, object], list[tuple[str, str]]]:
    forecastable: list[Request] = []
    forecasts: dict[str, object] = {}
    blocked: list[tuple[str, str]] = []
    for request in requests:
        user = data.user_by_id[request.user_id]
        try:
            forecast = build_forecast(
                user,
                data.events_by_user_id[request.user_id],
                converter,
                request.request_date,
                resolved_overrides=overrides,
            )
        except MissingForecastAmount as exc:
            blocked.append((request.request_id, str(exc)))
            continue
        forecastable.append(request)
        forecasts[request.request_id] = forecast
    return forecastable, forecasts, blocked


def run(mode: str, *, dataset_dir: Path, write_path: Path | None = None) -> dict[str, object]:
    data = load_dataset(dataset_dir)
    converter = CurrencyConverter(data.exchange_rates)
    overrides = load_resolved_overrides(Path("evaluation/resolved_overrides.json"))
    source = data.sample_requests if mode == "sample" else data.requests
    forecastable, forecasts, blocked = _forecastable_requests(source, data, converter, overrides)
    rows, errors = assemble_dataset(
        forecastable,
        users=data.user_by_id,
        options_by_request_id=data.options_by_request_id,
        events_by_user_id=data.events_by_user_id,
        forecasts_by_request_id=forecasts,
        converter=converter,
        explanation_generator=MockExplanationGenerator(),
        hard_error_path=Path("evaluation/phase5_errors.jsonl"),
    )
    if write_path is not None:
        write_output_csv(rows, write_path)
    report = {
        "mode": mode,
        "input_rows": len(source),
        "forecastable_rows": len(forecastable),
        "blocked_rows": len(blocked),
        "validator_pass_rows": len(rows),
        "validator_fail_rows": len(errors),
        "blocked_request_ids": [request_id for request_id, _ in blocked],
        "validation_errors": [{"request_id": request_id, "error": error} for request_id, error in errors],
        "status_distribution": dict(Counter(row.affordability_status for row in rows)),
        "method_distribution": dict(Counter(row.recommended_payment_method for row in rows)),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sample", "evaluation"), default="evaluation")
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--write", type=Path, default=None)
    args = parser.parse_args()
    report = run(args.mode, dataset_dir=args.dataset_dir, write_path=args.write)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["validator_fail_rows"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
