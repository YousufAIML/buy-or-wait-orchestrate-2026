"""Live Phase 6 Step 4 Groq decision-explanation batch.

This runner deliberately does not write output.csv. Numeric decisions are
computed first, then passed as immutable facts to Groq for wording only.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine.currency import CurrencyConverter
from src.engine.dataframe_lite import load_dataset
from src.engine.forecast import build_forecast
from src.engine.output import GroqExplanationGenerator, check_explanation_consistency
from src.engine.overrides import load_resolved_overrides
from src.engine.plans import construct_plan
from src.extraction.env import load_dotenv_manual
from src.extraction.llm_client import GroqClient
from src.extraction.usage import UsageTracker

MODEL = "openai/gpt-oss-120b"
OUTPUT_PATH = Path("evaluation/live_explanations.json")
LEDGER_PATH = Path("evaluation/usage_ledger.jsonl")
MIN_INTERVAL_SECONDS = 1.0


def _save(path: Path, document: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def run() -> dict:
    load_dotenv_manual()
    data = load_dataset("dataset")
    converter = CurrencyConverter(data.exchange_rates)
    overrides = load_resolved_overrides("evaluation/resolved_overrides.json")
    tracker = UsageTracker(LEDGER_PATH)
    client = GroqClient(model=MODEL, max_retries=2, timeout=60.0, usage_tracker=tracker)
    generator = GroqExplanationGenerator(client)
    started_at = datetime.now(timezone.utc)
    document = {
        "version": 1,
        "provider": "groq",
        "model": MODEL,
        "started_at": started_at.isoformat(),
        "explanations": {},
        "consistency_flags": {},
        "failures": {},
    }
    _save(OUTPUT_PATH, document)

    for index, request in enumerate(data.requests):
        user = data.user_by_id[request.user_id]
        try:
            forecast = build_forecast(
                user,
                data.events_by_user_id[request.user_id],
                converter,
                request.request_date,
                resolved_overrides=overrides,
            )
            decision = construct_plan(
                user,
                request,
                data.options_by_request_id.get(request.request_id, ()),
                data.events_by_user_id[request.user_id],
                forecast,
                converter,
            )
            explanation = generator.generate(request, decision)
            flags = check_explanation_consistency(explanation, request, decision)
            document["explanations"][request.request_id] = explanation
            if flags:
                document["consistency_flags"][request.request_id] = list(flags)
        except Exception as exc:
            document["failures"][request.request_id] = f"{type(exc).__name__}: {exc}"
        finally:
            _save(OUTPUT_PATH, document)
            if index + 1 < len(data.requests):
                time.sleep(MIN_INTERVAL_SECONDS)

    records = tracker.records()
    request_ids = {request.request_id for request in data.requests}
    batch_records = [
        record
        for record in records
        if record.provider == "groq"
        and record.model == MODEL
        and record.request_key in request_ids
        and record.timestamp >= started_at.isoformat()
    ]
    document["finished_at"] = datetime.now(timezone.utc).isoformat()
    document["usage"] = {
        "calls": len(batch_records),
        "input_tokens": sum(record.input_tokens for record in batch_records),
        "output_tokens": sum(record.output_tokens for record in batch_records),
        "total_tokens": sum(record.total_tokens for record in batch_records),
        "latency_ms": sum(record.latency_ms for record in batch_records),
        "average_latency_ms": (
            sum(record.latency_ms for record in batch_records) / len(batch_records)
            if batch_records
            else 0.0
        ),
    }
    _save(OUTPUT_PATH, document)
    examples = [
        {"request_id": request_id, "explanation": document["explanations"][request_id]}
        for request_id in sorted(document["explanations"])[:5]
    ]
    return {
        "requests": len(data.requests),
        "successful_explanations": len(document["explanations"]),
        "failures": document["failures"],
        "consistency_flags": document["consistency_flags"],
        "usage": document["usage"],
        "examples": examples,
        "output_path": str(OUTPUT_PATH),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
