"""Pytest layer mapping for acceptance/ specifications (strictly outside CI-L1..L3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = str(Path(__file__).resolve().parents[1] / "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def pytest_collection_modifyitems(config, items):
    root = Path(str(config.rootpath)).resolve()
    for item in items:
        item_path = Path(str(item.path)).resolve()
        try:
            rel_path = item_path.relative_to(root).as_posix()
        except ValueError:
            rel_path = item_path.as_posix()

        existing = {m.name for m in item.iter_markers()} & {"ci_l1", "ci_l2", "ci_l3", "live_l4", "perf"}
        if existing:
            continue

        if rel_path.startswith("acceptance/l4/"):
            item.add_marker(pytest.mark.live_l4)
        elif rel_path.startswith("acceptance/p8/"):
            item.add_marker(pytest.mark.perf)
