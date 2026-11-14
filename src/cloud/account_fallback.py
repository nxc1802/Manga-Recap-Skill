"""Try each configured token once; stop on exhaustion, never fake success."""
from functools import wraps
from src.cli.exit_codes import CliValidationError
from src.core.logger import logger
from src.core.storage import StorageError


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
            except (CliValidationError, ValueError, StorageError):
                # Input/approval/artifact errors must still fail closed immediately.
                raise
            except Exception:
                pass
            logger.warning("Kaggle account '%s' failed; trying the next token", self.account_name)
        raise RuntimeError("All configured Kaggle accounts failed; stopped without publishing success") from None
    return dispatch
