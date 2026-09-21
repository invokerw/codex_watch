"""Verify an installed wheel from a temporary directory without source imports."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, help="Python executable in the clean installation")
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    python = str(args.python.absolute())  # Preserve virtual-environment symlink.
    with tempfile.TemporaryDirectory(prefix="codex-watch-installed-") as temp:
        directory = Path(temp)
        shutil.copytree(root / "tests", directory / "tests")
        shutil.copytree(root / ".github", directory / ".github")
        env = dict(os.environ, CODEX_WATCH_TEST_INSTALLED="1", CODEX_WATCH_TEST_DIST=str(args.dist.resolve()),
                   CODEX_WATCH_HOME=str(directory / "user data"), PYTHONNOUSERSITE="1", PYTHONUTF8="1")
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        info = subprocess.run([python, "-c", "import json, codex_watch; print(json.dumps(codex_watch.__file__))"],
                              cwd=directory, env=env, capture_output=True, text=True, check=True)
        location = Path(json.loads(info.stdout)).resolve()
        if "site-packages" not in location.parts or root / "codex_watch" in location.parents:
            raise RuntimeError(f"Expected an installed package, got {location}")
        print(f"Testing installed package: {location}", flush=True)
        return subprocess.run([python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                              cwd=directory, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
