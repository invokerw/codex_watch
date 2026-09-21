#!/usr/bin/env python3
"""Compatibility launcher using this checkout's existing capture directory."""
from pathlib import Path
import sys

from codex_watch.cli import main as package_main

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    return package_main(argv, legacy_root=ROOT, cli_prefix=[sys.executable, str(ROOT / "codex_dump.py")])


if __name__ == "__main__":
    raise SystemExit(main())
