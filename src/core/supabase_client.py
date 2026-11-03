"""
Deprecated Supabase Client Stub for Manga Recap V3.
All data persistence has been migrated to local JSON storage (src.core.storage).
This stub prevents breaking legacy imports during transition and provides clear migration error messages.
"""
import warnings

warnings.warn(
    "src.core.supabase_client is deprecated and will be removed in PR-09. "
    "Use src.core.storage functions (read_json, atomic_write_json) instead.",
    DeprecationWarning,
    stacklevel=2,
)

class DeprecatedSupabaseClient:
    def __getattr__(self, name: str):
        raise RuntimeError(
            f"Supabase has been eradicated from Manga Recap V3. Access to '{name}' is unsupported. "
            "Please migrate to local JSON storage via src.core.storage."
        )

supabase = DeprecatedSupabaseClient()

def get_supabase_client(use_admin: bool = True):
    return supabase
