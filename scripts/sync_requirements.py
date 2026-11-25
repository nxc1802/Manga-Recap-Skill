#!/usr/bin/env python3
"""Generate requirements.txt directly from pyproject.toml [project.dependencies]."""

from __future__ import annotations

from pathlib import Path
import sys

try:
    import tomllib
except ImportError:  # Python 3.10 fallback
    import tomli as tomllib  # type: ignore


def generate_requirements(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    lines = [
        "# Generated from pyproject.toml [project.dependencies] via scripts/sync_requirements.py.",
        "# Do not edit manually; update pyproject.toml and re-run scripts/sync_requirements.py.",
        "",
        *deps,
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    content = generate_requirements(root)
    req_path = root / "requirements.txt"
    if argv and "--check" in argv:
        current = req_path.read_text(encoding="utf-8") if req_path.is_file() else ""
        if current != content:
            print("requirements.txt is out of sync with pyproject.toml", file=sys.stderr)
            return 1
        return 0
    req_path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
