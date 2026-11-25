"""Small local Kaggle token registry; no quota/session bookkeeping."""
from __future__ import annotations

import os
from pathlib import Path
import re

from src.core.errors import DomainValidationError
from src.core.storage import FileLock, atomic_write_json, read_json


class KaggleAccountManager:
    def __init__(self, registry_file=None):
        data_root = os.getenv("MANGA_RECAP_DATA_DIR")
        default = Path(data_root) / "credentials/kaggle_accounts.json" if data_root else Path("credentials/kaggle_accounts.json")
        self.registry_file = Path(registry_file or os.getenv("KAGGLE_ACCOUNTS_FILE") or default).resolve()

    def _load(self):
        if not self.registry_file.exists():
            return {"default_account": None, "accounts": {}}
        data = read_json(self.registry_file)
        accounts = data.get("accounts")
        if not isinstance(accounts, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.startswith("KGAT_") for k, v in accounts.items()):
            raise DomainValidationError("Invalid Kaggle account registry")
        if data.get("default_account") not in (None, *accounts):
            raise DomainValidationError("Unknown default Kaggle account")
        return data

    def _save(self, data):
        atomic_write_json(self.registry_file, data)  # mkstemp creates the file with 0600
        self.registry_file.chmod(0o600)

    def add(self, name, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name or "") or name.startswith("KGAT_"):
            raise DomainValidationError("Use a simple account name, not a token")
        if not re.fullmatch(r"KGAT_[A-Za-z0-9_-]+", (token or "").strip()):
            raise DomainValidationError("A Kaggle access token (KGAT_) is required")
        token = token.strip()
        with FileLock(self.registry_file):
            data = self._load()
            if not self.registry_file.exists():
                legacy = self._legacy_token()
                if legacy:
                    data = {"default_account": "existing", "accounts": {"existing": legacy}}
            for existing, value in data["accounts"].items():
                if value == token:
                    if not self.registry_file.exists():
                        self._save(data)
                    return existing
            if name in data["accounts"]:
                raise DomainValidationError("Account name already exists; remove it first")
            data["accounts"][name] = token
            if not data["default_account"]:
                data["default_account"] = name
            self._save(data)
        return name

    def remove(self, name):
        with FileLock(self.registry_file):
            data = self._load()
            if name not in data["accounts"]:
                raise DomainValidationError("Unknown Kaggle account")
            del data["accounts"][name]
            if data["default_account"] == name:
                data["default_account"] = next(iter(data["accounts"]), None)
            self._save(data)

    def set_default(self, name):
        with FileLock(self.registry_file):
            data = self._load()
            if name not in data["accounts"]:
                raise DomainValidationError("Unknown Kaggle account")
            data["default_account"] = name
            self._save(data)

    def list_accounts(self):
        data = self._load()
        return [{"account_name": name, "is_default": name == data["default_account"]}
                for name in data["accounts"]]

    def candidates(self):
        """Return default first, then remaining tokens; preserve legacy credentials."""
        data = self._load()
        names = list(data["accounts"])
        default = data["default_account"]
        if default:
            names.remove(default)
            names.insert(0, default)
        candidates = [(name, data["accounts"][name]) for name in names]
        if self.registry_file.exists():
            return candidates
        legacy = self._legacy_token()
        return [("existing", legacy)] if legacy else [("legacy", None)]

    @staticmethod
    def _legacy_token():
        legacy = os.getenv("KAGGLE_API_TOKEN")
        if not legacy and not os.getenv("MANGA_RECAP_DATA_DIR"):
            path = Path(os.getenv("KAGGLE_CONFIG_DIR", Path.home() / ".kaggle")) / "access_token"
            if path.is_file():
                legacy = path.read_text().strip()
        return legacy
