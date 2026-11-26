"""HTTP transport client for Gemini Decisions API (`gemini-decision-1`)."""

from __future__ import annotations

import base64
import io
import math
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
DECISION_USD_PER_1M_INPUT_TOKENS = 0.10
VALID_IMAGE_RESOLUTIONS = ("low", "medium", "high", "ultra_high")
CONTENT_BLOCKED_CODES = frozenset({"safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"})

_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{8,}")
_QUESTION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProviderContentBlockedError(ProviderError):
    """Raised when the Gemini Decisions API blocks an input under safety filters."""

    def __init__(self, message: str, *, code: str = "content_blocked", provider: str = PROVIDER_NAME) -> None:
        super().__init__(message, category="content_blocked", provider=provider, retryable=False)
        self.error_code = code


def redact_secret(text: Any, *secrets: str) -> str:
    """Scrubs explicit secrets and Google API key patterns from text."""
    result = str(text or "")
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = _KEY_PATTERN.sub("[REDACTED_API_KEY]", result)
    return result


def encode_image_jpeg(
    image_path: Path,
    *,
    max_height: int = 1024,
    max_width: Optional[int] = None,
    quality: int = 82,
) -> Dict[str, Any]:
    """Loads an image, applies bounded resize, and returns base64 JPEG plus metadata."""
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        orig_w, orig_h = rgb.size
        w, h = orig_w, orig_h
        scale = 1.0
        if max_height and h > max_height:
            scale = min(scale, max_height / float(h))
        if max_width and w > max_width:
            scale = min(scale, max_width / float(w))
        if scale < 1.0:
            rgb = rgb.resize(
                (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                Image.Resampling.LANCZOS,
            )
            w, h = rgb.size
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=int(quality))
    raw_bytes = buf.getvalue()
    return {
        "b64": base64.b64encode(raw_bytes).decode("ascii"),
        "jpeg_bytes": len(raw_bytes),
        "orig_width": orig_w,
        "orig_height": orig_h,
        "encoded_width": w,
        "encoded_height": h,
    }


def encode_image_b64(
    image_path: Path,
    max_height: int = 1024,
    *,
    max_width: Optional[int] = None,
    quality: int = 82,
) -> str:
    """Loads an image, downscales dimensions if needed, and returns base64 JPEG."""
    return str(
        encode_image_jpeg(
            image_path,
            max_height=max_height,
            max_width=max_width,
            quality=quality,
        )["b64"]
    )


def _validate_questions_payload(questions: Dict[str, Dict[str, Any]]) -> None:
    if not isinstance(questions, dict) or not (1 <= len(questions) <= 1000):
        raise ValueError("Decision request must include between 1 and 1000 questions.")
    for q_key, q_spec in questions.items():
        if not isinstance(q_key, str) or not _QUESTION_KEY_PATTERN.match(q_key):
            raise ValueError(f"Invalid Decision question key '{q_key}'.")
        if not isinstance(q_spec, dict):
            raise ValueError(f"Question spec for '{q_key}' must be a dict.")
        q_type = q_spec.get("type", "choice")
        if q_type == "choice":
            criteria = q_spec.get("criteria")
            if not isinstance(criteria, dict) or not (2 <= len(criteria) <= 255):
                raise ValueError(f"Choice question '{q_key}' must define 2..255 criteria options.")


def _validate_prob_map(probs: Any, allowed: Dict[str, Any], q_key: str) -> Dict[str, float]:
    if probs is None:
        return {}
    if not isinstance(probs, dict):
        raise ProviderIntegrityError(
            f"Invalid Decision API response: non-dict probabilities for '{q_key}'.",
            category="invalid_response",
            provider=PROVIDER_NAME,
        )
    out: Dict[str, float] = {}
    for k, v in probs.items():
        if not isinstance(k, str) or (allowed and k not in allowed):
            raise ProviderIntegrityError(
                f"Invalid Decision API response: unexpected probability key '{k}' for '{q_key}'.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise ProviderIntegrityError(
                f"Invalid Decision API response: non-finite probability for '{q_key}.{k}'.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        fv = float(v)
        if fv < -1e-6 or fv > 1.0 + 1e-6:
            raise ProviderIntegrityError(
                f"Invalid Decision API response: probability out of [0, 1] for '{q_key}.{k}': {fv}.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        out[k] = min(1.0, max(0.0, fv))
    return out


def _validate_confidence(val: Any, q_key: str) -> Optional[float]:
    if val is None:
        return None
    if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(float(val)):
        raise ProviderIntegrityError(
            f"Invalid Decision API response: non-finite confidence for '{q_key}'.",
            category="invalid_response",
            provider=PROVIDER_NAME,
        )
    fv = float(val)
    if fv < -1e-6 or fv > 1.0 + 1e-6:
        raise ProviderIntegrityError(
            f"Invalid Decision API response: confidence out of [0, 1] for '{q_key}': {fv}.",
            category="invalid_response",
            provider=PROVIDER_NAME,
        )
    return min(1.0, max(0.0, fv))


def extract_usage_metrics(payload: Any) -> Dict[str, Any]:
    """Parses Gemini Decisions API ``usage`` without confusing missing usage with 0."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) or "total_input_tokens" not in usage:
        return {
            "usage_known": False,
            "total_input_tokens": None,
            "text_tokens": None,
            "image_tokens": None,
            "total_output_tokens": None,
        }
    raw_total = usage.get("total_input_tokens")
    if not isinstance(raw_total, (int, float)) or isinstance(raw_total, bool) or not math.isfinite(float(raw_total)):
        return {
            "usage_known": False,
            "total_input_tokens": None,
            "text_tokens": None,
            "image_tokens": None,
            "total_output_tokens": None,
        }
    text_toks: Optional[int] = None
    image_toks: Optional[int] = None
    by_mod = usage.get("input_tokens_by_modality")
    if isinstance(by_mod, list):
        for item in by_mod:
            if not isinstance(item, dict):
                continue
            mod = str(item.get("modality", "")).lower()
            tok = item.get("tokens")
            if isinstance(tok, (int, float)) and not isinstance(tok, bool) and math.isfinite(float(tok)):
                if mod == "text":
                    text_toks = (text_toks or 0) + int(tok)
                elif mod == "image":
                    image_toks = (image_toks or 0) + int(tok)
    raw_out = usage.get("total_output_tokens")
    out_toks = int(raw_out) if isinstance(raw_out, (int, float)) and not isinstance(raw_out, bool) else 0
    return {
        "usage_known": True,
        "total_input_tokens": int(raw_total),
        "text_tokens": text_toks,
        "image_tokens": image_toks,
        "total_output_tokens": out_toks,
    }


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
        expected_type = q_spec.get("type", "choice")
        resp_type = ans_obj.get("type")
        if resp_type is not None and resp_type != expected_type:
            raise ProviderIntegrityError(
                f"Invalid Decision API response: unexpected answer type '{resp_type}' for '{q_key}'.",
                category="invalid_response",
                provider=PROVIDER_NAME,
            )
        _validate_confidence(ans_obj.get("confidence"), q_key)
        if expected_type == "noul":
            pval = ans_obj.get("noul")
            if not isinstance(pval, (int, float)) or isinstance(pval, bool) or not math.isfinite(float(pval)):
                raise ProviderIntegrityError(
                    f"Invalid Decision API response: invalid noul probability for '{q_key}'.",
                    category="invalid_response",
                    provider=PROVIDER_NAME,
                )
            if not (0.0 <= float(pval) <= 1.0):
                raise ProviderIntegrityError(
                    f"Invalid Decision API response: noul probability out of [0, 1] for '{q_key}'.",
                    category="invalid_response",
                    provider=PROVIDER_NAME,
                )
        elif expected_type == "score":
            sval = ans_obj.get("score")
            if not isinstance(sval, (int, float)) or isinstance(sval, bool) or not math.isfinite(float(sval)):
                raise ProviderIntegrityError(
                    f"Invalid Decision API response: invalid score for '{q_key}'.",
                    category="invalid_response",
                    provider=PROVIDER_NAME,
                )
            _validate_prob_map(ans_obj.get("probabilities"), {}, q_key)
        else:
            choice = ans_obj.get("choice")
            allowed = q_spec.get("criteria") or {}
            if not isinstance(choice, str) or (allowed and choice not in allowed):
                raise ProviderIntegrityError(
                    f"Invalid Decision API response: unexpected choice '{choice}' for '{q_key}'.",
                    category="invalid_response",
                    provider=PROVIDER_NAME,
                )
            _validate_prob_map(ans_obj.get("probabilities"), allowed, q_key)

    return payload


class DecisionClient:
    """Thread-safe httpx client for closed-set ``gemini-decision-1`` routing calls."""

    def __init__(
        self,
        *,
        endpoint: str = DECISIONS_ENDPOINT,
        model: str = DECISIONS_MODEL,
        timeout: float = 30.0,
        max_retries: int = 6,
        retry_base_seconds: float = 25.0,
        retry_step_seconds: float = 10.0,
        retry_jitter: float = 0.0,
        max_retry_sleep_seconds: float = 300.0,
        min_interval_seconds: float = 0.0,
        http_client: Optional[httpx.Client] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_step_seconds = float(retry_step_seconds)
        self.retry_jitter = max(0.0, float(retry_jitter))
        self.max_retry_sleep_seconds = max(0.0, float(max_retry_sleep_seconds))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._http_client = http_client
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._thread_local = threading.local()
        self._pool_lock = threading.Lock()
        self._owned_clients: List[httpx.Client] = []
        self._pace_lock = threading.Lock()
        self._cooldown_until: float = 0.0
        self._next_request_at: float = 0.0

    def __enter__(self) -> "DecisionClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._pool_lock:
            clients = list(self._owned_clients)
            self._owned_clients.clear()
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        self._thread_local = threading.local()

    def _get_client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        client = getattr(self._thread_local, "http_client", None)
        if client is None or getattr(client, "is_closed", False):
            client = httpx.Client(timeout=self.timeout)
            self._thread_local.http_client = client
            with self._pool_lock:
                self._owned_clients.append(client)
        return client

    def _wait_global_pacing(self, *, elapsed_until: float = 0.0) -> None:
        wait = 0.0
        with self._pace_lock:
            now = max(self._clock(), elapsed_until)
            target = max(self._cooldown_until, self._next_request_at)
            if target > now:
                wait = target - now
            if self.min_interval_seconds > 0.0:
                self._next_request_at = max(now, target) + self.min_interval_seconds
        if wait > 0.0:
            self._sleep(wait)

    def _notify_cooldown(self, wait_sec: float) -> float:
        if wait_sec <= 0.0:
            return 0.0
        with self._pace_lock:
            until = self._clock() + wait_sec
            if until > self._cooldown_until:
                self._cooldown_until = until
            return until

    def _retry_delay(self, resp: Optional[httpx.Response], attempt: int) -> float:
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                    if math.isfinite(parsed) and parsed >= 0:
                        return min(parsed, 120.0)
                except ValueError:
                    pass
        base = self.retry_base_seconds + attempt * self.retry_step_seconds
        if self.retry_jitter > 0.0:
            factor = random.uniform(max(0.1, 1.0 - self.retry_jitter), 1.0 + self.retry_jitter)
            return round(base * factor, 3)
        return base

    def call_input(
        self,
        *,
        input_blocks: List[Dict[str, Any]],
        questions: Dict[str, Dict[str, Any]],
        api_key: str,
        payload_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Executes a single Decision API HTTP request over prebuilt input blocks."""
        if not api_key or not str(api_key).strip():
            raise ProviderAuthError(
                "Missing GEMINI_API_KEY for Decision API request.",
                category="auth_failed",
                provider=PROVIDER_NAME,
            )
        _validate_questions_payload(questions)
        payload = {
            "model": self.model,
            "input": input_blocks,
            "questions": questions,
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        client = self._get_client()
        total_slept = 0.0
        retries = 0
        ambiguous_billed_attempts = 0
        elapsed_until = 0.0
        t_start = self._clock()

        for attempt in range(self.max_retries):
            self._wait_global_pacing(elapsed_until=elapsed_until)
            elapsed_until = 0.0
            try:
                resp = client.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                ambiguous_billed_attempts += 1
                if attempt < min(self.max_retries - 1, 2):
                    wait_sec = min(2.0 * (attempt + 1), 10.0)
                    if total_slept + wait_sec <= self.max_retry_sleep_seconds:
                        logger.warning(
                            f"Decision API timeout (attempt {attempt + 1}/{self.max_retries}); retrying in {wait_sec:.1f}s..."
                        )
                        retries += 1
                        total_slept += wait_sec
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
                ambiguous_billed_attempts += 1
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
                validated = dict(_validate_decision_response(parsed, questions))
                latency_ms = round(max(0.0, (self._clock() - t_start) * 1000.0), 2)
                usage_meta = extract_usage_metrics(validated)
                validated["_meta"] = {
                    "latency_ms": latency_ms,
                    "retries": retries,
                    "ambiguous_attempts": ambiguous_billed_attempts,
                    "payload_bytes": payload_bytes,
                    **usage_meta,
                }
                return validated

            snippet = redact_secret(resp.text[:300], api_key)
            if resp.status_code in (401, 403):
                raise ProviderAuthError(
                    f"Gemini Decisions API authentication failed ({resp.status_code}): {snippet}",
                    category="auth_failed",
                    provider=PROVIDER_NAME,
                    retryable=False,
                )

            if resp.status_code == 400:
                err_code = ""
                try:
                    err_json = resp.json()
                    if isinstance(err_json, dict) and isinstance(err_json.get("error"), dict):
                        err_code = str(err_json["error"].get("code") or "").strip().lower()
                except Exception:
                    err_code = ""
                if err_code in CONTENT_BLOCKED_CODES:
                    raise ProviderContentBlockedError(
                        f"Gemini Decisions API blocked request ({err_code}): {snippet}",
                        code=err_code,
                    )

            if resp.status_code == 429:
                if attempt < self.max_retries - 1:
                    wait_sec = self._retry_delay(resp, attempt)
                    if total_slept + wait_sec <= self.max_retry_sleep_seconds:
                        logger.warning(
                            f"Decision API 429 rate limit; sleeping {wait_sec:.1f}s "
                            f"(attempt {attempt + 1}/{self.max_retries})..."
                        )
                        retries += 1
                        total_slept += wait_sec
                        elapsed_until = self._notify_cooldown(wait_sec)
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
                if total_slept + wait_sec <= self.max_retry_sleep_seconds:
                    logger.warning(
                        f"Decision API {resp.status_code} server error; sleeping {wait_sec:.1f}s "
                        f"(attempt {attempt + 1}/{self.max_retries})..."
                    )
                    retries += 1
                    total_slept += wait_sec
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

    def call(
        self,
        *,
        image_path: Path,
        text_context: str,
        questions: Dict[str, Dict[str, Any]],
        api_key: str,
        max_height: int = 1024,
        max_width: Optional[int] = None,
        quality: int = 82,
        image_resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes a single Decision API HTTP request over one image + text context."""
        if not api_key or not str(api_key).strip():
            raise ProviderAuthError(
                "Missing GEMINI_API_KEY for Decision API request.",
                category="auth_failed",
                provider=PROVIDER_NAME,
            )
        if image_resolution is not None and image_resolution not in VALID_IMAGE_RESOLUTIONS:
            raise ValueError(f"Unsupported Decision image_resolution '{image_resolution}'.")

        enc = encode_image_jpeg(
            image_path,
            max_height=max_height,
            max_width=max_width,
            quality=quality,
        )
        img_block: Dict[str, Any] = {"type": "image", "data": enc["b64"], "mime_type": "image/jpeg"}
        if image_resolution is not None:
            img_block["resolution"] = image_resolution
        res = self.call_input(
            input_blocks=[img_block, {"type": "text", "text": text_context}],
            questions=questions,
            api_key=api_key,
            payload_bytes=enc["jpeg_bytes"],
        )
        meta = res.get("_meta")
        if isinstance(meta, dict):
            meta["encoded_width"] = enc["encoded_width"]
            meta["encoded_height"] = enc["encoded_height"]
            meta["orig_width"] = enc["orig_width"]
            meta["orig_height"] = enc["orig_height"]
        return res


def call_gemini_decision(
    *,
    image_path: Path,
    text_context: str,
    questions: Dict[str, Dict[str, Any]],
    api_key: str,
    max_height: int = 1024,
    max_width: Optional[int] = None,
    quality: int = 82,
    image_resolution: Optional[str] = None,
    timeout: float = 30.0,
    max_retries: int = 6,
    client: Optional[DecisionClient] = None,
) -> Dict[str, Any]:
    """Functional wrapper over ``DecisionClient.call`` for backward compatibility."""
    if client is not None:
        return client.call(
            image_path=image_path,
            text_context=text_context,
            questions=questions,
            api_key=api_key,
            max_height=max_height,
            max_width=max_width,
            quality=quality,
            image_resolution=image_resolution,
        )
    with DecisionClient(timeout=timeout, max_retries=max_retries) as transport:
        return transport.call(
            image_path=image_path,
            text_context=text_context,
            questions=questions,
            api_key=api_key,
            max_height=max_height,
            max_width=max_width,
            quality=quality,
            image_resolution=image_resolution,
        )
