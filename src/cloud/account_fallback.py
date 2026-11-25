"""Try each configured token cleanly on pre-execution quota/queue rejection; never replay post-submit ambiguity."""
from functools import wraps
from src.cloud.lifecycle import NON_RETRYABLE_FALLBACK_CATEGORIES
from src.core.errors import (
    DomainValidationError,
    ProviderError,
    ProviderIntegrityError,
    StorageError,
)
from src.core.logger import logger
from src.providers.kaggle import KaggleClientPollTimeoutError, KaggleSubmitAmbiguousError


def with_account_fallback(operation):
    @wraps(operation)
    def dispatch(self, *args, **kwargs):
        # Legacy isolated adapters may inject a client without constructing a pool.
        candidates = getattr(self, "_accounts", None)
        if not candidates:
            return operation(self, *args, **kwargs)
        start = self._account_index
        order = list(range(start, len(candidates))) + list(range(start))
        for index in order:
            if index != self._account_index:
                self._select_account(index)
            if not self.is_authenticated:
                continue
            try:
                result = operation(self, *args, **kwargs)
                if result and not (
                    isinstance(result, dict) and (result.get("status") in {"error", "failed"} or result.get("success") is False)
                ):
                    return result
            except (
                DomainValidationError,
                ValueError,
                StorageError,
                ProviderIntegrityError,
                KaggleSubmitAmbiguousError,
                KaggleClientPollTimeoutError,
            ):
                # Input/approval/integrity/post-submit-ambiguous errors must fail closed immediately.
                raise
            except ProviderError as exc:
                if getattr(exc, "category", None) in NON_RETRYABLE_FALLBACK_CATEGORIES:
                    raise
                logger.warning("Kaggle account '%s' failed (%s); trying the next token", self.account_name, type(exc).__name__)
                continue
            except Exception as exc:
                logger.warning("Kaggle account '%s' failed (%s); trying the next token", self.account_name, type(exc).__name__)
                continue
            logger.warning("Kaggle account '%s' failed; trying the next token", self.account_name)
        raise RuntimeError("All configured Kaggle accounts failed; stopped without publishing success") from None
    return dispatch
