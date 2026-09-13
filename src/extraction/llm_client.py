"""Provider-agnostic urllib clients for Gemini and Groq REST APIs.

API keys are intentionally read only from environment variables at call time.
No key is accepted as a constructor argument and no key literal/default exists.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .usage import UsageTracker


class LLMClientError(RuntimeError):
    """Base class for loud provider/client failures."""


class ConfigurationError(LLMClientError):
    """Raised when a required environment variable is missing."""


class ProviderHTTPError(LLMClientError):
    """Raised for non-retryable HTTP or malformed provider responses."""

    def __init__(self, message: str, *, status: Optional[int] = None, headers: Any = None, body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.response_headers = dict(headers.items()) if hasattr(headers, "items") else dict(headers or {})
        self.response_body = body


class ProviderTransportError(LLMClientError):
    """Raised after retryable transport failures are exhausted."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    provider: str
    model: str
    text: str
    raw: dict[str, Any]
    input_tokens: int
    output_tokens: int
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: bytes = b""
    latency_ms: float = 0.0


Opener = Callable[..., Any]
Sleeper = Callable[[float], None]


def _retry_after(headers: Any, attempt: int) -> float:
    value = None
    if headers is not None:
        value = headers.get("Retry-After") or headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(30.0, 0.5 * (2**attempt))


def _read_response(response: Any) -> bytes:
    try:
        return response.read()
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


class _BaseClient:
    provider = ""
    env_var = ""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        opener: Optional[Opener] = None,
        sleeper: Optional[Sleeper] = None,
        usage_tracker: Optional[UsageTracker] = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.timeout = timeout
        self.max_retries = max_retries
        self._opener = opener or urllib.request.urlopen
        self._sleeper = sleeper or time.sleep
        self.usage_tracker = usage_tracker or UsageTracker()

    def _api_key(self) -> str:
        key = os.environ.get(self.env_var)
        if not key:
            raise ConfigurationError(f"required environment variable {self.env_var} is missing")
        if key != key.strip() or any(character.isspace() for character in key):
            raise ConfigurationError(f"environment variable {self.env_var} contains whitespace")
        return key

    def _post_json(
        self,
        request: urllib.request.Request,
        *,
        request_key: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, str], bytes]:
        last_transport: Optional[BaseException] = None
        model = getattr(self, "model", "unknown")
        for attempt in range(self.max_retries + 1):
            try:
                response = self._opener(request, timeout=self.timeout)
                status = getattr(response, "status", 200)
                headers = getattr(response, "headers", None)
                body = _read_response(response)
                if status == 429:
                    if attempt < self.max_retries:
                        self._sleeper(_retry_after(headers, attempt))
                        continue
                    error = f"{self.provider} HTTP 429 after {self.max_retries + 1} attempts"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderHTTPError(error, status=status, headers=headers, body=body)
                if status >= 400:
                    error = f"{self.provider} HTTP {status}: {body[:500].decode('utf-8', 'replace')}"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderHTTPError(error, status=status, headers=headers, body=body)
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    error = f"{self.provider} returned invalid JSON"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderHTTPError(error, status=status, headers=headers, body=body) from exc
                if not isinstance(parsed, dict):
                    error = f"{self.provider} returned a non-object JSON response"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderHTTPError(error, status=status, headers=headers, body=body)
                header_map = dict(headers.items()) if hasattr(headers, "items") else dict(headers or {})
                return parsed, header_map, body
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                if exc.code == 429 and attempt < self.max_retries:
                    self._sleeper(_retry_after(exc.headers, attempt))
                    continue
                if exc.code == 429:
                    error = f"{self.provider} HTTP 429 after {self.max_retries + 1} attempts"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderHTTPError(error, status=exc.code, headers=exc.headers, body=body) from exc
                error = f"{self.provider} HTTP {exc.code}: {body[:500].decode('utf-8', 'replace')}"
                self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                raise ProviderHTTPError(error, status=exc.code, headers=exc.headers, body=body) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
                last_transport = exc
                if attempt >= self.max_retries:
                    error = f"{self.provider} transport failure after {self.max_retries + 1} attempts: {exc}"
                    self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
                    raise ProviderTransportError(error) from exc
                self._sleeper(_retry_after(None, attempt))
        error = f"{self.provider} request failed: {last_transport}"
        self.usage_tracker.record_error(self.provider, model, error, request_key=request_key)
        raise ProviderTransportError(error)

    def _record(self, response: LLMResponse, request_key: Optional[str]) -> None:
        self.usage_tracker.record(
            self.provider,
            response.model,
            response.input_tokens,
            response.output_tokens,
            request_key=request_key,
            latency_ms=response.latency_ms,
        )


class GeminiClient(_BaseClient):
    provider = "gemini"
    env_var = "GEMINI_API_KEY"

    def __init__(self, *, model: str = "gemini-3.6-flash", **kwargs: Any):
        super().__init__(**kwargs)
        self.model = model

    def generate_content(
        self,
        prompt: str,
        *,
        image_bytes: Optional[bytes] = None,
        mime_type: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> LLMResponse:
        if image_bytes is not None and not mime_type:
            raise ValueError("mime_type is required when image_bytes is supplied")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image_bytes is not None:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )
        body = {"contents": [{"parts": parts}]}
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model, safe='')}:generateContent"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key(),
            },
            method="POST",
        )
        started = time.perf_counter()
        raw, response_headers, response_body = self._post_json(request, request_key=request_key)
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            parts_out = raw["candidates"][0]["content"]["parts"]
            text = "".join(part["text"] for part in parts_out if "text" in part)
            usage = raw["usageMetadata"]
            input_tokens = int(usage["promptTokenCount"])
            output_tokens = int(usage.get("candidatesTokenCount", 0))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderHTTPError("Gemini response lacks text or usageMetadata") from exc
        response = LLMResponse(
            self.provider,
            self.model,
            text,
            raw,
            input_tokens,
            output_tokens,
            response_headers,
            response_body,
            latency_ms,
        )
        self._record(response, request_key)
        return response


class GroqClient(_BaseClient):
    provider = "groq"
    env_var = "GROQ_API_KEY"

    def __init__(self, *, model: Optional[str] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.model = model or os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        request_key: Optional[str] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key()}",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        started = time.perf_counter()
        raw, response_headers, response_body = self._post_json(request, request_key=request_key)
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            text = raw["choices"][0]["message"]["content"]
            usage = raw["usage"]
            input_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderHTTPError("Groq response lacks message content or usage") from exc
        response = LLMResponse(
            self.provider,
            self.model,
            text,
            raw,
            input_tokens,
            output_tokens,
            response_headers,
            response_body,
            latency_ms,
        )
        self._record(response, request_key)
        return response
