"""Full Phase 2 evidence extraction batch.

Run as ``python3 -m src.extraction.batch``. The dotenv loader is called before
provider clients are constructed, and the batch never writes secrets.
"""

from __future__ import annotations

from .env import load_dotenv_manual

load_dotenv_manual()

import json
import time
from pathlib import Path
from typing import Any

from src.engine.dataframe_lite import load_dataset

from .cache import ExtractionCache
from .handlers import ExtractionAuditLog, ImageAmountExtractor, MessageClassifier
from .llm_client import GeminiClient, GroqClient
from .usage import UsageTracker


DEFAULT_OVERRIDES_PATH = Path("evaluation/resolved_overrides.json")
DEFAULT_CACHE_PATH = Path("evaluation/extraction_cache.sqlite3")
DEFAULT_AUDIT_PATH = Path("evaluation/extraction_audit.jsonl")
DEFAULT_LEDGER_PATH = Path("evaluation/usage_ledger.jsonl")
GEMINI_MIN_INTERVAL_SECONDS = 4.5
GROQ_MIN_INTERVAL_SECONDS = 2.0


def _restore_validated_audit_cache(cache: ExtractionCache, audit_path: Path) -> int:
    """Restore prior validated smoke results so a known item is not re-called."""
    if not audit_path.exists():
        return 0
    restored = 0
    with audit_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") != "validated" or not isinstance(record.get("parsed"), dict):
                continue
            kind = record.get("kind")
            item_id = record.get("item_id")
            if kind not in {"image_amount", "message_classification"} or not item_id:
                continue
            if cache.get(kind, item_id) is None:
                cache.put(kind, item_id, record["parsed"])
                restored += 1
    return restored


def _load_or_initialize(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "image_amounts": {},
            "message_classifications": {},
            "failures": [],
        }
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("version") != 1:
        raise ValueError("resolved overrides file must have version 1")
    document.setdefault("image_amounts", {})
    document.setdefault("message_classifications", {})
    document.setdefault("failures", [])
    return document


def _persist(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def run_image_batch(
    dataset_dir: Path | str = Path("dataset"),
    overrides_path: Path | str = DEFAULT_OVERRIDES_PATH,
    *,
    fixed_delay_seconds: float = 5.0,
    gemini_max_retries: int = 2,
) -> dict[str, Any]:
    """Resolve only blank-amount images, one uncached call at a time.

    The fixed delay is applied after every live attempt, including failures;
    provider Retry-After sleeps happen inside the client before this delay.
    """
    if fixed_delay_seconds < 0:
        raise ValueError("fixed_delay_seconds cannot be negative")
    if gemini_max_retries < 0:
        raise ValueError("gemini_max_retries cannot be negative")
    dataset_dir = Path(dataset_dir)
    overrides_path = Path(overrides_path)
    data = load_dataset(dataset_dir)
    document = _load_or_initialize(overrides_path)
    tracker = UsageTracker(DEFAULT_LEDGER_PATH)
    cache = ExtractionCache(DEFAULT_CACHE_PATH)
    audit = ExtractionAuditLog(DEFAULT_AUDIT_PATH)
    restored = _restore_validated_audit_cache(cache, DEFAULT_AUDIT_PATH)
    extractor = ImageAmountExtractor(
        GeminiClient(max_retries=gemini_max_retries, timeout=30.0, usage_tracker=tracker), cache, audit
    )
    image_targets = [
        event
        for event in data.events
        if event.amount is None and event.event_id in data.images_by_event_id
    ]
    failures: list[dict[str, str]] = []
    cached_count = 0
    attempts = 0
    try:
        for event in image_targets:
            if cache.get("image_amount", event.event_id) is not None:
                cached_count += 1
                continue
            attempts += 1
            try:
                image = data.images_by_event_id[event.event_id][0]
                image_path = dataset_dir / "media" / "images" / f"{image.image_id}.png"
                result = extractor.extract(event, image_path, request_key=event.event_id)
                document["image_amounts"][event.event_id] = {
                    "amount": str(result.amount),
                    "currency": result.currency,
                    "confidence": str(result.confidence),
                    "justification": result.justification,
                }
            except Exception as exc:
                failures.append({
                    "kind": "image_amount",
                    "item_id": event.event_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            finally:
                _persist(overrides_path, document)
                time.sleep(fixed_delay_seconds)
    finally:
        cache.close()

    document["failures"] = [
        failure
        for failure in document.get("failures", [])
        if failure.get("kind") != "image_amount"
    ] + failures
    _persist(overrides_path, document)
    return {
        "images_targeted": len(image_targets),
        "images_resolved": len(document["image_amounts"]),
        "images_cached": cached_count,
        "image_attempts": attempts,
        "image_failures": failures,
        "audit_cache_restored": restored,
        "fixed_delay_seconds": fixed_delay_seconds,
        "gemini_max_retries": gemini_max_retries,
        "overrides_path": str(overrides_path),
    }


def run_batch(
    dataset_dir: Path | str = Path("dataset"),
    overrides_path: Path | str = DEFAULT_OVERRIDES_PATH,
) -> dict[str, Any]:
    dataset_dir = Path(dataset_dir)
    overrides_path = Path(overrides_path)
    data = load_dataset(dataset_dir)
    document = _load_or_initialize(overrides_path)
    tracker = UsageTracker(DEFAULT_LEDGER_PATH)
    cache = ExtractionCache(DEFAULT_CACHE_PATH)
    audit = ExtractionAuditLog(DEFAULT_AUDIT_PATH)
    restored = _restore_validated_audit_cache(cache, DEFAULT_AUDIT_PATH)
    image_extractor = ImageAmountExtractor(
        GeminiClient(max_retries=2, timeout=30.0, usage_tracker=tracker), cache, audit
    )
    message_classifier = MessageClassifier(
        GroqClient(max_retries=2, timeout=30.0, usage_tracker=tracker), cache, audit
    )

    image_targets = [
        event
        for event in data.events
        if event.amount is None and event.event_id in data.images_by_event_id
    ]
    message_targets = [message for message in data.messages if message.related_event_id]
    failures: list[dict[str, str]] = []
    image_cached = 0
    message_cached = 0
    last_gemini_call: float | None = None
    last_groq_call: float | None = None
    try:
        for event in image_targets:
            if cache.get("image_amount", event.event_id) is None:
                if last_gemini_call is not None:
                    time.sleep(max(0.0, GEMINI_MIN_INTERVAL_SECONDS - (time.monotonic() - last_gemini_call)))
                last_gemini_call = time.monotonic()
            image = data.images_by_event_id[event.event_id][0]
            image_path = dataset_dir / "media" / "images" / f"{image.image_id}.png"
            try:
                result = image_extractor.extract(event, image_path, request_key=event.event_id)
                image_cached += int(result.cached)
                document["image_amounts"][event.event_id] = {
                    "amount": str(result.amount),
                    "currency": result.currency,
                    "confidence": str(result.confidence),
                    "justification": result.justification,
                }
            except Exception as exc:
                failure = {"kind": "image_amount", "item_id": event.event_id, "error": f"{type(exc).__name__}: {exc}"}
                failures.append(failure)
            _persist(overrides_path, document)

        for message in message_targets:
            if cache.get("message_classification", message.message_id) is None:
                if last_groq_call is not None:
                    time.sleep(max(0.0, GROQ_MIN_INTERVAL_SECONDS - (time.monotonic() - last_groq_call)))
                last_groq_call = time.monotonic()
            try:
                result = message_classifier.classify(message, request_key=message.message_id)
                message_cached += int(result.cached)
                document["message_classifications"][message.message_id] = {
                    "action": result.action,
                    "confidence": str(result.confidence),
                    "justification": result.justification,
                    "accepted": result.accepted,
                }
            except Exception as exc:
                failure = {"kind": "message_classification", "item_id": message.message_id, "error": f"{type(exc).__name__}: {exc}"}
                failures.append(failure)
            _persist(overrides_path, document)
    finally:
        cache.close()

    document["failures"] = failures
    _persist(overrides_path, document)
    return {
        "images_targeted": len(image_targets),
        "images_resolved": len(document["image_amounts"]),
        "images_cached": image_cached,
        "messages_targeted": len(message_targets),
        "messages_classified": len(document["message_classifications"]),
        "messages_cached": message_cached,
        "failures": failures,
        "audit_cache_restored": restored,
        "overrides_path": str(overrides_path),
    }


def main() -> int:
    summary = run_batch()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
