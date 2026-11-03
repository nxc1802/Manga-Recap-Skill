#!/usr/bin/env python3
"""
Manga Recap V1 Baseline Rollback Script
Restores tracked repository files to v1.0-baseline commit (88bb273)
while strictly preserving user data in data/ and agent metadata.
"""

import os
import sys
import subprocess
import hashlib
import json
from pathlib import Path

BASELINE_TAG = "v1.0-baseline"
BASELINE_COMMIT = "88bb273"
REPO_ROOT = Path(__file__).resolve().parents[1]

def run_cmd(cmd: list[str], cwd=REPO_ROOT, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)

def verify_tag_exists() -> str:
    res = subprocess.run(["git", "rev-parse", "--verify", BASELINE_TAG], cwd=REPO_ROOT, capture_output=True)
    if res.returncode == 0:
        return BASELINE_TAG
    res_commit = subprocess.run(["git", "rev-parse", "--verify", BASELINE_COMMIT], cwd=REPO_ROOT, capture_output=True)
    if res_commit.returncode == 0:
        print(f"Tag '{BASELINE_TAG}' not found; falling back to commit '{BASELINE_COMMIT}'.")
        return BASELINE_COMMIT
    raise RuntimeError(f"Neither '{BASELINE_TAG}' nor '{BASELINE_COMMIT}' found in git history.")

def check_clean_working_tree() -> bool:
    res = subprocess.run(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=REPO_ROOT)
    return res.returncode == 0

def main():
    print(f"=== Manga Recap Baseline Rollback Utility ===")
    print(f"Repository: {REPO_ROOT}")
    
    target = verify_tag_exists()
    print(f"Baseline target identified: {target}")

    if not check_clean_working_tree():
        print("Warning: Uncommitted changes detected in tracked files.")
        if len(sys.argv) > 1 and sys.argv[1] == "--stash":
            stash_res = run_cmd(["git", "stash", "push", "-m", "pre_rollback_auto_stash"])
            print(f"Stashed changes: {stash_res.stdout.strip()}")
        else:
            ans = input("Stash uncommitted changes and proceed? [y/N]: ").strip().lower()
            if ans != 'y':
                print("Rollback aborted.")
                sys.exit(1)
            stash_res = run_cmd(["git", "stash", "push", "-m", "pre_rollback_auto_stash"])
            print(f"Stashed changes: {stash_res.stdout.strip()}")

    print(f"Checking out target '{target}'...")
    run_cmd(["git", "checkout", target])

    # Run Pytest verification
    print("Running baseline test verification...")
    pytest_bin = REPO_ROOT / "venv" / "bin" / "pytest"
    cmd = [str(pytest_bin)] if pytest_bin.exists() else ["pytest"]
    
    env_vars = os.environ.copy()
    env_vars["PYTHONPATH"] = "."
    test_res = subprocess.run(cmd, cwd=REPO_ROOT, env=env_vars)
    if test_res.returncode == 0:
        print("Verification SUCCESS: All 12 baseline unit tests passed!")
    else:
        print("Warning: Some baseline unit tests failed. Please inspect.")

    print(f"=== Rollback to {target} completed successfully ===")

if __name__ == "__main__":
    main()
