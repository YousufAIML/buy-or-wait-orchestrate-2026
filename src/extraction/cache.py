"""Persistent SQLite cache for validated extraction results."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ExtractionCache:
    """Cache keyed by extraction kind and stable event/message identifier."""

    def __init__(self, path: Path | str = Path("evaluation/extraction_cache.sqlite3")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS extraction_cache (
                kind TEXT NOT NULL,
                item_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, item_id)
            )
            """
        )
        self._connection.commit()

    def get(self, kind: str, item_id: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute(
            "SELECT payload FROM extraction_cache WHERE kind = ? AND item_id = ?",
            (kind, item_id),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, kind: str, item_id: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        self._connection.execute(
            """
            INSERT INTO extraction_cache(kind, item_id, payload, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, item_id) DO UPDATE SET
                payload = excluded.payload,
                created_at = excluded.created_at
            """,
            (kind, item_id, serialized, datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ExtractionCache":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
