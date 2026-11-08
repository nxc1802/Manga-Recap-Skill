"""
src/crawler/registry.py
SiteAdapterRegistry: manages domain profiles and resolution hierarchy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from src.core.logger import logger
from src.core.storage import atomic_write_json, get_project_dir, read_json
from src.crawler.schemas import SourceProfile


class SiteAdapterRegistry:
    """Manages and caches domain-specific SourceProfile definitions."""

    def __init__(self, builtin_dir: Optional[Path] = None):
        self.builtin_dir = builtin_dir or (Path(__file__).parent / "profiles")
        self._cache: Dict[str, SourceProfile] = {}

    def extract_domain(self, domain_or_url: str) -> str:
        """Extracts clean domain name from URL or host string."""
        if "://" in domain_or_url:
            domain_or_url = urlparse(domain_or_url).netloc
        domain = domain_or_url.split(":")[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def get_profile(
        self,
        domain_or_url: str,
        project_slug: Optional[str] = None,
    ) -> SourceProfile:
        """
        Retrieves matching SourceProfile following resolution hierarchy:
        1. data/projects/<slug>/source_profiles/<domain>.json
        2. data/source_profiles/<domain>.json
        3. src/crawler/profiles/<domain>.json (built-in)
        4. Generic fallback adapter
        """
        domain = self.extract_domain(domain_or_url)
        cache_key = f"{project_slug or 'global'}:{domain}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        candidate_paths: List[Path] = []

        # 1. Project-level custom profile
        if project_slug:
            try:
                proj_dir = get_project_dir(project_slug)
                candidate_paths.append(proj_dir / "source_profiles" / f"{domain}.json")
            except Exception:
                pass

        # 2. Global data-level profile
        candidate_paths.append(Path("data/source_profiles") / f"{domain}.json")

        # 3. Built-in profile
        candidate_paths.append(self.builtin_dir / f"{domain}.json")

        for p in candidate_paths:
            if p.exists():
                try:
                    data = read_json(p)
                    profile = SourceProfile.model_validate(data)
                    self._cache[cache_key] = profile
                    logger.debug(f"Loaded source profile for '{domain}' from {p}")
                    return profile
                except Exception as e:
                    logger.warning(f"Failed to load profile from {p}: {e}")

        # 4. Fallback to generic profile
        generic_path = self.builtin_dir / "generic.json"
        if generic_path.exists():
            try:
                data = read_json(generic_path)
                data["domain"] = domain
                data["name"] = f"Generic ({domain})"
                profile = SourceProfile.model_validate(data)
                self._cache[cache_key] = profile
                return profile
            except Exception as e:
                logger.warning(f"Failed to load generic profile from {generic_path}: {e}")

        # In-memory default
        profile = SourceProfile(
            domain=domain,
            name=f"Default ({domain})",
            confidence=0.5,
        )
        self._cache[cache_key] = profile
        return profile

    def save_profile(
        self,
        profile: SourceProfile,
        project_slug: Optional[str] = None,
    ) -> Path:
        """Persists a SourceProfile to project or global directory."""
        domain = self.extract_domain(profile.domain)
        if project_slug:
            target_dir = get_project_dir(project_slug) / "source_profiles"
        else:
            target_dir = Path("data/source_profiles")

        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{domain}.json"
        atomic_write_json(target_path, profile)

        cache_key = f"{project_slug or 'global'}:{domain}"
        self._cache[cache_key] = profile
        logger.info(f"Saved source profile for '{domain}' to {target_path}")
        return target_path

    def list_profiles(self, project_slug: Optional[str] = None) -> List[SourceProfile]:
        """Lists all registered profiles across built-in, global, and project scopes."""
        found_domains = set()
        profiles = []

        dirs_to_check = [self.builtin_dir, Path("data/source_profiles")]
        if project_slug:
            try:
                dirs_to_check.insert(0, get_project_dir(project_slug) / "source_profiles")
            except Exception:
                pass

        for d in dirs_to_check:
            if d.exists():
                for p in d.glob("*.json"):
                    domain = p.stem.lower()
                    if domain not in found_domains and domain != "generic":
                        found_domains.add(domain)
                        try:
                            prof = SourceProfile.model_validate(read_json(p))
                            profiles.append(prof)
                        except Exception:
                            pass
        return profiles
