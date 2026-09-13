"""Append-only model usage ledger."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional


@dataclass(frozen=True, slots=True)
class UsageRecord:
    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: str
    cache_hit: bool = False
    request_key: Optional[str] = None
    latency_ms: float = 0.0


class UsageTracker:
    """Write one JSON object per call; failures are not silently swallowed."""

    def __init__(self, path: Path | str = Path("evaluation/usage_ledger.jsonl")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cost_usd: Decimal = Decimal("0"),
        cache_hit: bool = False,
        request_key: Optional[str] = None,
        latency_ms: float = 0.0,
    ) -> UsageRecord:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        if latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=str(cost_usd),
            cache_hit=cache_hit,
            request_key=request_key,
            latency_ms=float(latency_ms),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return record

    def record_error(
        self,
        provider: str,
        model: str,
        error: str,
        *,
        request_key: Optional[str] = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "provider": provider,
            "model": model,
            "error": error,
            "request_key": request_key,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def records(self) -> list[UsageRecord]:
        if not self.path.exists():
            return []
        result = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("status") == "error":
                        continue
                    result.append(UsageRecord(**data))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid usage ledger line {line_number}") from exc
        return result
