"""
tests/unit/test_p13_test_pyramid.py
P13 Standardized Test Pyramid verification (CI-L1, CI-L2, CI-L3, LIVE-L4, PERF-P8/P9).
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path
from typing import List, Set

import pytest
import yaml

pytestmark = pytest.mark.ci_l1

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_PYRAMID_MARKERS = ("ci_l1", "ci_l2", "ci_l3", "live_l4", "perf")
DETERMINISTIC_MARKERS = ("ci_l1", "ci_l2", "ci_l3")


def _collect_nodeids(*args: str, allow_empty: bool = False) -> List[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    allowed_codes = {0, 5} if allow_empty else {0}
    if proc.returncode not in allowed_codes:
        raise AssertionError(
            f"pytest --collect-only {' '.join(args)} failed with exit {proc.returncode}:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    nodeids: List[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if "::" in stripped and not stripped.startswith(("=", "-")):
            nodeids.append(stripped)
    return nodeids


def test_pytest_ini_registers_all_five_pyramid_markers():
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "pytest.ini", encoding="utf-8")
    assert cfg.has_section("pytest")
    assert cfg.get("pytest", "testpaths").strip() == "tests"
    assert "--strict-markers" in cfg.get("pytest", "addopts")

    markers_raw = cfg.get("pytest", "markers")
    registered = {
        line.split(":", 1)[0].strip()
        for line in markers_raw.splitlines()
        if line.strip() and ":" in line
    }
    assert set(ALL_PYRAMID_MARKERS).issubset(registered)


def test_deterministic_tests_partition_100_percent_and_disjoint():
    all_tests = _collect_nodeids()
    assert len(all_tests) >= 890, f"Expected >=890 tests under tests/, got {len(all_tests)}"

    by_layer: dict[str, Set[str]] = {}
    for layer in DETERMINISTIC_MARKERS:
        by_layer[layer] = set(_collect_nodeids("-m", layer))
        assert len(by_layer[layer]) > 0, f"Layer {layer} must not be empty"

    # Pairwise disjoint
    assert by_layer["ci_l1"].isdisjoint(by_layer["ci_l2"])
    assert by_layer["ci_l1"].isdisjoint(by_layer["ci_l3"])
    assert by_layer["ci_l2"].isdisjoint(by_layer["ci_l3"])

    # Exact union == 100% of tests/
    union_l1_l2_l3 = by_layer["ci_l1"] | by_layer["ci_l2"] | by_layer["ci_l3"]
    assert union_l1_l2_l3 == set(all_tests)

    combined = set(_collect_nodeids("-m", "ci_l1 or ci_l2 or ci_l3"))
    assert combined == set(all_tests)

    # Zero live_l4 or perf items inside deterministic tests/
    assert _collect_nodeids("-m", "live_l4", allow_empty=True) == []
    assert _collect_nodeids("-m", "perf", allow_empty=True) == []

    # Verify no acceptance/ nodeid ever leaks into deterministic tests/
    assert not any(nid.startswith("acceptance/") for nid in all_tests)


def test_acceptance_specs_stay_outside_deterministic_ci_and_map_to_live_l4_and_perf():
    l4_nodes = _collect_nodeids("acceptance/l4", "-o", "python_files=*_spec.py", "-m", "live_l4")
    perf_nodes = _collect_nodeids("acceptance/p8", "-o", "python_files=*_spec.py", "-m", "perf")

    assert len(l4_nodes) > 0
    assert len(perf_nodes) > 0
    assert all(nid.startswith("acceptance/l4/") for nid in l4_nodes)
    assert all(nid.startswith("acceptance/p8/") for nid in perf_nodes)

    # Confirm acceptance/ has zero items in deterministic layers
    assert _collect_nodeids(
        "acceptance", "-o", "python_files=*_spec.py", "-m", "ci_l1 or ci_l2 or ci_l3", allow_empty=True
    ) == []


def test_all_test_files_registered_without_conflicts():
    import conftest as tests_conftest
    PYRAMID_FILE_LAYERS = tests_conftest.PYRAMID_FILE_LAYERS
    PYRAMID_NODE_PREFIX_LAYERS = tests_conftest.PYRAMID_NODE_PREFIX_LAYERS

    test_files = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "tests").rglob("test_*.py")
    }
    covered_by_prefix = {prefix.split("::", 1)[0] for prefix in PYRAMID_NODE_PREFIX_LAYERS}
    registered_files = set(PYRAMID_FILE_LAYERS) | covered_by_prefix

    missing = test_files - registered_files
    assert not missing, f"Unregistered test files: {sorted(missing)}"

    # Ensure every collected item has exactly one pyramid marker via pytest runtime hooks
    all_nodeids = set(_collect_nodeids())
    assert len(all_nodeids) == sum(len(_collect_nodeids("-m", m)) for m in DETERMINISTIC_MARKERS)


def test_v4_smoke_workflow_splits_ci_layers_without_always_masking():
    wf_path = REPO_ROOT / ".github" / "workflows" / "v4-smoke.yml"
    raw = wf_path.read_text(encoding="utf-8")
    assert "if: always()" not in raw, "Workflow must never use `if: always()` to mask prior failures"

    wf = yaml.safe_load(raw)
    jobs = wf["jobs"]
    assert {"ci-l1", "ci-l2", "ci-l3"}.issubset(set(jobs))

    l1_steps = "\n".join(str(s.get("run", "")) for s in jobs["ci-l1"]["steps"])
    l2_steps = "\n".join(str(s.get("run", "")) for s in jobs["ci-l2"]["steps"])
    l3_steps = "\n".join(str(s.get("run", "")) for s in jobs["ci-l3"]["steps"])

    assert "compileall" in l1_steps
    assert "git diff --check" in l1_steps
    assert "freeze_contracts.py --check" in l1_steps
    assert "pytest -m ci_l1" in l1_steps
    assert "pytest -m ci_l2" in l2_steps
    assert "pytest -m ci_l3" in l3_steps


def test_v4_live_l4_workflow_is_manual_dispatch_only_with_required_secrets():
    wf_path = REPO_ROOT / ".github" / "workflows" / "v4-live-l4.yml"
    raw = wf_path.read_text(encoding="utf-8")
    wf = yaml.safe_load(raw)

    # PyYAML parses bare `on:` as boolean True or string "on"
    on_cfg = wf.get("on") if "on" in wf else wf.get(True)
    assert isinstance(on_cfg, dict)
    assert list(on_cfg.keys()) == ["workflow_dispatch"]

    jobs = wf["jobs"]
    assert "live-l4" in jobs
    assert "perf-p8-p9" in jobs

    for token in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY", "HF_TOKEN", "YOUTUBE_CLIENT_SECRETS_JSON", "YOUTUBE_TOKEN_JSON"):
        assert token in raw
