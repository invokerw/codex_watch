"""Run the same checks against source or an installed wheel."""
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def cli_command():
    if os.environ.get("CODEX_WATCH_TEST_INSTALLED") == "1":
        name = "codex-watch.exe" if os.name == "nt" else "codex-watch"
        return [str(Path(sys.executable).parent / name)]
    return [sys.executable, str(ROOT / "codex_dump.py")]
