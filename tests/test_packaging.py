"""Packaging boundaries, default paths and installed CLI behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import codex_watch
from codex_watch.platform_support import data_directory
from tests.test_support import cli_command


class PackagingTests(unittest.TestCase):
    def test_platform_data_directories(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/test-user")):
            with patch("sys.platform", "darwin"):
                self.assertEqual(data_directory(), Path("/test-user/Library/Application Support/codex-watch"))
            with patch("sys.platform", "linux"):
                self.assertEqual(data_directory(), Path("/test-user/.local/share/codex-watch"))
                with patch.dict(os.environ, {"XDG_DATA_HOME": "/data"}):
                    self.assertEqual(data_directory(), Path("/data/codex-watch"))
                with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                    self.assertTrue(data_directory().is_absolute())
            with patch("sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": "/local-apps"}):
                self.assertEqual(data_directory(), Path("/local-apps/codex-watch"))

    def test_legacy_root_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            legacy, override = Path(temp) / "legacy", Path(temp) / "custom"
            self.assertEqual(data_directory(legacy), legacy.resolve())
            with patch.dict(os.environ, {"CODEX_WATCH_HOME": str(override)}):
                self.assertEqual(data_directory(legacy), override.resolve())

    def test_version(self):
        result = subprocess.run([*cli_command(), "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), f"codex-watch {codex_watch.__version__}")

    def test_doctor_data_dir_precedence_and_no_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data with spaces"
            env = dict(os.environ, CODEX_WATCH_HOME=str(Path(temp) / "ignored"))
            result = subprocess.run([*cli_command(), "--data-dir", str(path), "doctor", "--json"],
                                    env=env, capture_output=True, text=True)
            self.assertIn(result.returncode, {0, 1}, result.stderr)
            info = json.loads(result.stdout)
            self.assertEqual(info["data_dir"], str(path.resolve()))
            self.assertFalse(info["ca_exists"])
            self.assertFalse(path.exists())
            if os.environ.get("CODEX_WATCH_TEST_INSTALLED") == "1":
                self.assertIn("site-packages", info["package_dir"])
                self.assertEqual(info["mitmproxy"], "11.1.3")

    def test_wheel_contains_only_package_and_metadata(self):
        dist = Path(os.environ.get("CODEX_WATCH_TEST_DIST", Path(__file__).resolve().parents[1] / "dist"))
        wheel = dist / f"codex_watch-{codex_watch.__version__}-py3-none-any.whl"
        if not wheel.exists():
            self.skipTest("Build the wheel first to inspect its contents")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assertIn("codex_watch/addon.py", names)
            self.assertIn("codex_watch/__main__.py", names)
            for name in names:
                self.assertTrue(name.startswith(("codex_watch/", f"codex_watch-{codex_watch.__version__}.dist-info/")), name)
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.endswith((".jsonl", ".sqlite3", ".pem", ".key", ".pyc")), name)
            metadata = archive.read(f"codex_watch-{codex_watch.__version__}.dist-info/METADATA").decode()
            self.assertIn("Requires-Dist: mitmproxy==11.1.3", metadata)
            self.assertIn("codex-watch = codex_watch.cli:main", archive.read(
                f"codex_watch-{codex_watch.__version__}.dist-info/entry_points.txt").decode())

    def test_source_archive_excludes_local_data(self):
        dist = Path(os.environ.get("CODEX_WATCH_TEST_DIST", Path(__file__).resolve().parents[1] / "dist"))
        path = dist / f"codex_watch-{codex_watch.__version__}.tar.gz"
        if not path.exists():
            self.skipTest("Build the source archive first to inspect its contents")
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                self.assertFalse(set(parts) & {"captures", ".mitmproxy", ".venv", ".package-test", ".build-cache", "__pycache__"}, member.name)


if __name__ == "__main__":
    unittest.main()
