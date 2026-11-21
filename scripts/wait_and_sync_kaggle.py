#!/usr/bin/env python3
"""Resume a previously dispatched Kaggle job using its local cloud_job.json."""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.cli.main import main

    raise SystemExit(main(["cloud", "pull", *sys.argv[1:]]))
