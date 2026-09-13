"""Minimal stdlib-only .env loading for local development.

Call load_dotenv_manual() once at the beginning of an entry-point script, before
constructing provider clients. Existing environment values always win.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_dotenv_manual(path: Path | str = ".env") -> tuple[str, ...]:
    """Load KEY=VALUE lines without overwriting existing environment values.

    Blank lines and lines whose first non-whitespace character is ``#`` are
    ignored. Malformed non-comment lines raise ValueError rather than being
    silently ignored, so a missing key cannot be hidden by a typo.
    """
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return ()
    loaded: list[str] = []
    with dotenv_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"{dotenv_path}:{line_number}: expected KEY=VALUE")
            key, value = line.split("=", 1)
            key = key.strip()
            value = _unquote(value.strip())
            if not _KEY_RE.fullmatch(key):
                raise ValueError(f"{dotenv_path}:{line_number}: invalid environment key")
            if key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return tuple(loaded)
