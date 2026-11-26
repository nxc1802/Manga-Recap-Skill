"""Account fallback strictly for explicitly classified safe pre-submit failures (never quota/timeout/unknown)."""
from functools import wraps
from src.cloud.lifecycle import NON_RETRYABLE_FALLBACK_CATEGORIES, SAFE_PRE_SUBMIT_FALLBACK_CATEGORIES
from src.core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
)
from src.core.logger import logger


def _is_safe_pre_submit_fallback(exc: BaseException) -> bool:
    """Allows rotating accounts ONLY on explicit pre-submit auth or non-quota rejection errors."""
    if isinstance(exc, ProviderQuotaError):
        return False
    if isinstance(exc, ProviderAuthError):
        return True
    if isinstance(exc, ProviderError):
        category = getattr(exc, "category", None)
        if category in NON_RETRYABLE_FALLBACK_CATEGORIES:
            return False
        if category in SAFE_PRE_SUBMIT_FALLBACK_CATEGORIES:
            return "quota" not in str(exc).lower()
    return False


def with_account_fallback(operation):
    @wraps(operation)
    def dispatch(self, *args, **kwargs):
        # Legacy isolated adapters may inject a client without constructing a pool.
        candidates = getattr(self, "_accounts", None)
        if not candidates:
            return operation(self, *args, **kwargs)
        start = self._account_index
        order = list(range(start, len(candidates))) + list(range(start))
        last_exc: BaseException | None = None
        for index in order:
            if index != self._account_index:
                self._select_account(index)
            if not self.is_authenticated:
                continue
            try:
                return operation(self, *args, **kwargs)
            except Exception as exc:
                if _is_safe_pre_submit_fallback(exc):
                    last_exc = exc
                    logger.warning(
                        "Kaggle account '%s' rejected pre-submit (%s); trying the next account",
                        self.account_name,
                        getattr(exc, "category", type(exc).__name__),
                    )
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No authenticated Kaggle account available for pre-submit dispatch") from None
    return dispatch
