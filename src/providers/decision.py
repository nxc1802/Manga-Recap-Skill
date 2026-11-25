"""HTTP transport client for Gemini Decisions API (`gemini-decision-1`)."""

from __future__ import annotations

import base64
import io
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx
from PIL import Image

from src.core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderIntegrityError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from src.core.logger import logger

DECISIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
DECISIONS_MODEL = "gemini-decision-1"
PROVIDER_NAME = "gemini-decision"

_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{8,}")


def redact_secret(text: Any, *secrets: str) -> str:
    """Scrubs explicit secrets and Google API key patterns from text."""
    result = str(text or "")
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = _KEY_PATTERN.sub("[REDACTED_API_KEY]", result)
    return result


def encode_image_b64(image_path: Path, max_height: int = 1024) -> str:
    """Loads an image, downscales height if needed, and returns base64 JPEG."""
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        if h > max_height:
            scale = max_height / float(h)
            rgb = rgb.resize(
                (max(1, int(round(w * scale))), max_height),
                Image.Resampling.LANCZOS,
            )
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _validate_decision_response(
    payload: Any,
    questions: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Validates closed-set response structure returned by Gemini Decisions API."""
    if not isinstance(payload, dict):
        raise ProviderIntegrityError(
            "Invalid Decision API response: expected top-level JSON object.",
            category="invalid_response",
            provider=PROVIDER_NAME,
        )
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ProviderIntegrityError(
            "Invalid Decision API response: missing or non-dict 'answers' field.",
            category="invalid_response",
            provider=PROVIDER_NAME,
        )

    for q_key, q_spec in questions.items():
        if q_key not in answers:
            raise ProviderIntegrityError(
                f"Invalid Decision API response: missing answer for '{q_key}'.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        ans_obj = answers.get(q_key)
        if not isinstance(ans_obj, dict):
            raise ProviderIntegrityError(
                f"Invalid Decision API response: answer for '{q_key}' must be an object.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        choice = ans_obj.get("choice")
        allowed = q_spec.get("criteria") or {}
        if not isinstance(choice, str) or (allowed and choice not in allowed):
            raise ProviderIntegrityError(
                f"Invalid Decision API response: unexpected choice '{choice}' for '{q_key}'.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )

    return payload


class DecisionClient:
    """Thin httpx client for closed-set ``gemini-decision-1`` routing calls."""

    def __init__(
        self,
        *,
        endpoint: str = DECISIONS_ENDPOINT,
        model: str = DECISIONS_MODEL,
        timeout: float = 30.0,
        max_retries: int = 6,
        retry_base_seconds: float = 25.0,
        retry_step_seconds: float = 10.0,
        http_client: Optional[httpx.Client] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_step_seconds = float(retry_step_seconds)
        self._http_client = http_client
        self._sleep = sleep_fn

    def _retry_delay(self, resp: Optional[httpx.Response], attempt: int) -> float:
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                    if parsed >= 0:
                        return min(parsed, 120.0)
                except ValueError:
                    pass
        return self.retry_base_seconds + attempt * self.retry_step_seconds

    def call(
        self,
        *,
        image_path: Path,
        text_context: str,
        questions: Dict[str, Dict[str, Any]],
        api_key: str,
        max_height: int = 1024,
    ) -> Dict[str, Any]:
        """Executes a single Decision API HTTP request with bounded retry/backoff."""
        if not api_key or not str(api_key).strip():
            raise ProviderAuthError(
                "Missing GEMINI_API_KEY for Decision API request.",
                category="auth_failed",
                provider=PROVIDER_NAME,
            )

        b64 = encode_image_b64(image_path, max_height=max_height)
        payload = {
            "model": self.model,
            "input": [
                {"type": "image", "data": b64, "mime_type": "image/jpeg"},
                {"type": "text", "text": text_context},
            ],
            "questions": questions,
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=self.timeout)
        try:
            for attempt in range(self.max_retries):
                try:
                    resp = client.post(
                        self.endpoint,
                        headers=headers,
                        json=payload,
                        timeout=self.timeout,
                    )
                except httpx.TimeoutException as exc:
                    if attempt < min(self.max_retries - 1, 2):
                        wait_sec = min(2.0 * (attempt + 1), 10.0)
                        logger.warning(
                            f"Decision API timeout (attempt {attempt + 1}/{self.max_retries}); retrying in {wait_sec:.1f}s..."
                        )
                        self._sleep(wait_sec)
                        continue
                    raise ProviderTimeoutError(
                        f"Gemini Decisions API timed out after {attempt + 1} attempt(s): "
                        f"{redact_secret(exc, api_key)}",
                        category="timeout",
                        provider=PROVIDER_NAME,
                        retryable=True,
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ProviderError(
                        f"Gemini Decisions API transport error: {redact_secret(exc, api_key)}",
                        category="transport_error",
                        provider=PROVIDER_NAME,
                        retryable=True,
                    ) from exc

                if resp.status_code == 200:
                    try:
                        parsed = resp.json()
                    except Exception as exc:
                        raise ProviderIntegrityError(
                            f"Invalid JSON returned by Decision API: {redact_secret(exc, api_key)}",
                            category="invalid_response",
                            provider=PROVIDER_NAME,
                        ) from exc
                    return _validate_decision_response(parsed, questions)

                snippet = redact_secret(resp.text[:300], api_key)
                if resp.status_code in (401, 403):
                    raise ProviderAuthError(
                        f"Gemini Decisions API authentication failed ({resp.status_code}): {snippet}",
                        category="auth_failed",
                        provider=PROVIDER_NAME,
                        retryable=False,
                    )

                if resp.status_code == 429:
                    if attempt < self.max_retries - 1:
                        wait_sec = self._retry_delay(resp, attempt)
                        logger.warning(
                            f"Decision API 429 rate limit; sleeping {wait_sec:.1f}s "
                            f"(attempt {attempt + 1}/{self.max_retries})..."
                        )
                        self._sleep(wait_sec)
                        continue
                    raise ProviderRateLimitError(
                        f"Gemini Decisions API exhausted retries on 429: {snippet}",
                        category="rate_limited",
                        provider=PROVIDER_NAME,
                        retryable=True,
                    )

                if 500 <= resp.status_code < 600 and attempt < self.max_retries - 1:
                    wait_sec = min(2.0 ** (attempt + 1), 16.0)
                    logger.warning(
                        f"Decision API {resp.status_code} server error; sleeping {wait_sec:.1f}s "
                        f"(attempt {attempt + 1}/{self.max_retries})..."
                    )
                    self._sleep(wait_sec)
                    continue

                if 400 <= resp.status_code < 500:
                    raise ProviderError(
                        f"Gemini Decisions API rejected request ({resp.status_code}): {snippet}",
                        category="invalid_request",
                        provider=PROVIDER_NAME,
                        retryable=False,
                    )

                raise ProviderError(
                    f"Gemini Decisions API error ({resp.status_code}): {snippet}",
                    category="server_error",
                    provider=PROVIDER_NAME,
                    retryable=True,
                )

            raise ProviderRateLimitError(
                "Gemini Decisions API exhausted retries.",
                category="rate_limited",
                provider=PROVIDER_NAME,
                retryable=True,
            )
        finally:
            if owns_client:
                client.close()


def call_gemini_decision(
    *,
    image_path: Path,
    text_context: str,
    questions: Dict[str, Dict[str, Any]],
    api_key: str,
    max_height: int = 1024,
    timeout: float = 30.0,
    max_retries: int = 6,
    client: Optional[DecisionClient] = None,
) -> Dict[str, Any]:
    """Functional wrapper over ``DecisionClient.call`` for backward compatibility."""
    transport = client or DecisionClient(timeout=timeout, max_retries=max_retries)
    return transport.call(
        image_path=image_path,
        text_context=text_context,
        questions=questions,
        api_key=api_key,
        max_height=max_height,
    )
