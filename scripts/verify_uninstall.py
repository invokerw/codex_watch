"""Install and self-uninstall disposable uv tools; never use the user's tool directory."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required")
    with tempfile.TemporaryDirectory(prefix="codex-watch-uninstall-") as temp:
        directory = Path(temp)
        data = directory / "data"
        tool_dir, tool_bin = directory / "tools", directory / "bin"
        env = dict(os.environ, UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(tool_bin),
                   UV_CACHE_DIR=str(args.cache_dir.resolve()), CODEX_WATCH_HOME=str(data),
                   PYTHONNOUSERSITE="1", PYTHONUTF8="1")
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)

        def run(command):
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout

        assert Path(run([uv, "tool", "dir", "--offline", "--no-config"]).strip()) == tool_dir
        assert Path(run([uv, "tool", "dir", "--bin", "--offline", "--no-config"]).strip()) == tool_bin
        executable = tool_bin / ("codex-watch.exe" if os.name == "nt" else "codex-watch")
        for keep in (False, True):
            run([uv, "tool", "install", "--offline", "--no-config", "--python", str(args.python.absolute()), str(args.wheel.resolve())])
            capture = data / "captures/codex.jsonl"
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_text("disposable fixture only")
            certificate = data / ".mitmproxy/test.pem"
            certificate.parent.mkdir(parents=True, exist_ok=True)
            certificate.write_text("disposable fixture only")
            run([str(executable), "uninstall", "--dry-run"])
            assert capture.exists() and executable.exists()
            command = [str(executable), "uninstall", "--yes"]
            if keep:
                command.append("--keep-data")
            output = run(command)
            assert not executable.exists(), output
            assert not (tool_dir / "codex-watch").exists(), output
            assert capture.exists() == keep, output
            assert certificate.exists() == keep, output
            print(f"PASS: real uv self-uninstall, keep_data={keep}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
