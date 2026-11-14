"""Compatibility entrypoint. All execution passes through canonical CLI policy."""
import sys
from src.cli.main import main

if __name__ == "__main__":
    sys.exit(main(["render", *sys.argv[1:]]))
