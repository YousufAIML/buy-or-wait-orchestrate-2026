"""Resume the Phase 6 Groq explanation batch in bounded chunks."""

from __future__ import annotations

import argparse
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
from src.engine.output import (
    GroqExplanationGenerator,
    check_explanation_consistency,
    normalize_explanation,
)
from src.engine.overrides import load_resolved_overrides
from src.engine.plans import construct_plan
from src.extraction.env import load_dotenv_manual
from src.extraction.llm_client import GroqClient
from src.extraction.usage import UsageTracker

MODEL = "openai/gpt-oss-120b"
OUTPUT_PATH = Path("evaluation/live_explanations.json")
LEDGER_PATH = Path("evaluation/usage_ledger.jsonl")
MIN_INTERVAL_SECONDS = 1.0


def _save(document: dict) -> None:
    temporary = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(OUTPUT_PATH)


def _decision(data, request, converter, overrides):
    user = data.user_by_id[request.user_id]
    forecast = build_forecast(
        user,
        data.events_by_user_id[request.user_id],
        converter,
        request.request_date,
        resolved_overrides=overrides,
    )
    return user, construct_plan(
        user,
        request,
        data.options_by_request_id.get(request.request_id, ()),
        data.events_by_user_id[request.user_id],
        forecast,
        converter,
    )


def _repair_existing(document: dict, data, converter, overrides) -> None:
    """Repair only presentation tokens and recheck flags; never call a provider."""
    for request_id, explanation in list(document.get("explanations", {}).items()):
        request = next(request for request in data.requests if request.request_id == request_id)
        _, decision = _decision(data, request, converter, overrides)
        normalized = normalize_explanation(explanation)
        document["explanations"][request_id] = normalized
        flags = check_explanation_consistency(normalized, request, decision)
        if flags:
            document.setdefault("consistency_flags", {})[request_id] = list(flags)
        else:
            document.setdefault("consistency_flags", {}).pop(request_id, None)


def run(limit: int) -> dict:
    if limit <= 0 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    load_dotenv_manual()
    data = load_dataset("dataset")
    converter = CurrencyConverter(data.exchange_rates)
    overrides = load_resolved_overrides("evaluation/resolved_overrides.json")
    tracker = UsageTracker(LEDGER_PATH)
    document = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    document.setdefault("explanations", {})
    document.setdefault("consistency_flags", {})
    document.setdefault("failures", {})

    _repair_existing(document, data, converter, overrides)
    _save(document)

    pending = [request for request in data.requests if request.request_id not in document["explanations"]]
    chunk = pending[:limit]
    started_at = datetime.now(timezone.utc).isoformat()
    client = GroqClient(model=MODEL, max_retries=2, timeout=60.0, usage_tracker=tracker)
    generator = GroqExplanationGenerator(client)
    for index, request in enumerate(chunk):
        try:
            _, decision = _decision(data, request, converter, overrides)
            explanation = generator.generate(request, decision)
            flags = check_explanation_consistency(explanation, request, decision)
            document["explanations"][request.request_id] = explanation
            if flags:
                document["consistency_flags"][request.request_id] = list(flags)
            else:
                document["consistency_flags"].pop(request.request_id, None)
        except Exception as exc:
            document["failures"][request.request_id] = f"{type(exc).__name__}: {exc}"
        finally:
            _save(document)
            if index + 1 < len(chunk):
                time.sleep(MIN_INTERVAL_SECONDS)

    records = tracker.records()
    chunk_ids = {request.request_id for request in chunk}
    batch_records = [
        record
        for record in records
        if record.provider == "groq"
        and record.model == MODEL
        and record.request_key in chunk_ids
        and record.timestamp >= started_at
    ]
    return {
        "chunk_requested": len(chunk),
        "chunk_successes": sum(request.request_id in document["explanations"] for request in chunk),
        "chunk_failures": {key: document["failures"][key] for key in chunk_ids if key in document["failures"]},
        "total_explanations": len(document["explanations"]),
        "total_flags": len(document["consistency_flags"]),
        "calls": len(batch_records),
        "input_tokens": sum(record.input_tokens for record in batch_records),
        "output_tokens": sum(record.output_tokens for record in batch_records),
        "total_tokens": sum(record.total_tokens for record in batch_records),
        "latency_ms": sum(record.latency_ms for record in batch_records),
        "average_latency_ms": sum(record.latency_ms for record in batch_records) / len(batch_records) if batch_records else 0.0,
        "processed_ids": [request.request_id for request in chunk],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(run(args.limit), indent=2, sort_keys=True))
