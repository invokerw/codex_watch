"""Run the same checks against source or an installed wheel."""
import os
from pathlib import Path
import sys


def cli_command():
    if os.environ.get("CODEX_WATCH_TEST_INSTALLED") == "1":
        name = "codex-watch.exe" if os.name == "nt" else "codex-watch"
        return [str(Path(sys.executable).parent / name)]
    return [sys.executable, str(Path(__file__).resolve().parent / "codex_dump.py")]
