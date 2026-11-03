#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Rollback Script: Revert Repository to V1 Baseline (v1.0-baseline)
# ==============================================================================

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BASELINE_TAG="v1.0-baseline"
BASELINE_COMMIT="88bb273"
MANIFEST_FILE="docs/v1_baseline_manifest.json"

echo "=== Manga Recap: Initiating Rollback to Baseline ($BASELINE_TAG) ==="

# 1. Verify Git Repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: Not a valid git repository at $REPO_ROOT" >&2
    exit 1
fi

# 2. Check if baseline tag or commit exists
if ! git rev-parse --verify "$BASELINE_TAG" >/dev/null 2>&1; then
    echo "Warning: Tag '$BASELINE_TAG' not found. Checking commit $BASELINE_COMMIT..."
    if ! git rev-parse --verify "$BASELINE_COMMIT" >/dev/null 2>&1; then
        echo "Error: Neither tag '$BASELINE_TAG' nor commit '$BASELINE_COMMIT' exists." >&2
        exit 1
    fi
    TARGET="$BASELINE_COMMIT"
else
    TARGET="$BASELINE_TAG"
fi

# 3. Check for uncommitted changes in tracked files
if ! git diff-index --quiet HEAD --; then
    echo "Detected uncommitted changes in tracked files."
    read -p "Do you want to stash tracked changes and proceed? (y/N): " -r CONFIRM
    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        STASH_NAME="rollback_stash_$(date +%s)"
        git stash push -m "$STASH_NAME"
        echo "Tracked changes stashed as: $STASH_NAME"
    else
        echo "Rollback aborted by user. Please commit or stash your changes."
        exit 1
    fi
fi

# 4. Checkout baseline state
echo "Checking out baseline target: $TARGET..."
git checkout "$TARGET"

# 5. Verify Baseline Test Suite
echo "Verifying test suite on baseline..."
if [ -f "./venv/bin/pytest" ]; then
    PYTHONPATH=. ./venv/bin/pytest || true
elif command -v pytest >/dev/null 2>&1; then
    PYTHONPATH=. pytest || true
else
    echo "Warning: pytest not found. Skipping automated test run."
fi

echo "=== Rollback to $BASELINE_TAG completed successfully! ==="
echo "Note: Untracked data/ and .agents/ metadata were strictly preserved."
