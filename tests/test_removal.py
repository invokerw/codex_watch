"""Exercise destructive operations only against disposable test directories."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_watch.lifecycle import data_locks
from codex_watch.monitor import ChatIndex
from codex_watch.removal import removal_targets, run_removal, uv_uninstaller
from tests.test_support import cli_command


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-watch-removal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app data"
        self.captures = self.root / "captures"
        self.captures.mkdir(parents=True)
        (self.captures / "codex.jsonl").write_text("disposable fixture")
        self.cert = self.root / ".mitmproxy"
        self.cert.mkdir()
        (self.cert / "test.pem").write_text("disposable fixture")

    def run_removal(self, command="purge", **kwargs):
        args = SimpleNamespace(command=command, keep_data=False, dry_run=False, yes=True)
        for key, value in kwargs.items():
            setattr(args, key, value)
        output = io.StringIO()
        with redirect_stdout(output):
            result = run_removal(args, self.root)
        return result, output.getvalue()

    def test_purge_removes_capture_index_and_cert_but_keeps_unrelated_files(self):
        index = ChatIndex(self.captures / "monitor/index.sqlite3")
        index.close()
        marker = self.root / "my-project.py"
        marker.write_text("preserve")
        result, _ = self.run_removal()
        self.assertEqual(result, 0)
        self.assertFalse(self.captures.exists())
        self.assertFalse(self.cert.exists())
        self.assertEqual(marker.read_text(), "preserve")

    def test_empty_data_root_is_removed_and_repeated_purge_is_safe(self):
        self.run_removal()
        self.assertFalse(self.root.exists())
        self.assertEqual(self.run_removal()[0], 0)

    def test_dry_run_does_not_remove_data(self):
        _, output = self.run_removal(dry_run=True)
        self.assertIn(str(self.captures), output)
        self.assertTrue(self.captures.exists())
        self.assertTrue(self.cert.exists())

    def test_confirmation_cancellation_leaves_data(self):
        with patch("builtins.input", return_value="no"):
            _, output = self.run_removal(yes=False)
        self.assertIn("已取消", output)
        self.assertTrue(self.captures.exists())

    def test_confirmation_eof_cancels(self):
        with patch("builtins.input", side_effect=EOFError):
            self.run_removal(yes=False)
        self.assertTrue(self.captures.exists())

    def test_uninstall_includes_data_cleanup_by_default(self):
        with patch("codex_watch.removal.uv_uninstaller", return_value=["uv", "tool", "uninstall", "codex-watch"]), \
             patch("codex_watch.removal.subprocess.run", return_value=SimpleNamespace(returncode=0)) as call:
            _, output = self.run_removal(command="uninstall")
        self.assertFalse(self.root.exists())
        self.assertIn("已卸载", output)
        self.assertEqual(call.call_args.args[0][:3], ["uv", "tool", "uninstall"])

    def test_wrong_installation_is_rejected_before_deleting_data(self):
        with patch("codex_watch.removal.uv_uninstaller", side_effect=ValueError("wrong environment")):
            with self.assertRaisesRegex(ValueError, "wrong environment"):
                self.run_removal(command="uninstall")
        self.assertTrue(self.captures.exists())

    def test_uv_failure_reports_that_data_has_already_been_cleaned(self):
        with patch("codex_watch.removal.uv_uninstaller", return_value=["uv"]), \
             patch("codex_watch.removal.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="locked", stdout="")):
            with self.assertRaisesRegex(ValueError, "数据已清理，但程序卸载失败"):
                self.run_removal(command="uninstall")
        self.assertFalse(self.captures.exists())

    def test_keep_data_removes_only_program(self):
        with patch("codex_watch.removal.uv_uninstaller", return_value=["uv"]), \
             patch("codex_watch.removal.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            self.run_removal(command="uninstall", keep_data=True)
        self.assertTrue(self.captures.exists())
        self.assertTrue(self.cert.exists())

    def test_root_and_home_directories_are_rejected(self):
        for path in (Path(self.root.anchor), Path.home()):
            with self.assertRaises(ValueError):
                removal_targets(path)

    def test_symlink_inside_data_is_not_followed_or_deleted(self):
        external = Path(self.temp.name) / "keep.txt"
        external.write_text("keep")
        link = self.captures / "linked.txt"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.run_removal()
        self.assertEqual(external.read_text(), "keep")
        self.assertTrue(self.cert.exists())

    def test_symlink_as_capture_directory_is_rejected(self):
        original = self.root / "captures-real"
        self.captures.rename(original)
        try:
            self.captures.symlink_to(original, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.run_removal()
        self.assertTrue((original / "codex.jsonl").exists())

    def test_running_monitor_or_proxy_blocks_cleanup(self):
        for role in ("watch", "serve"):
            with data_locks(self.root, (role,)):
                with self.assertRaisesRegex(ValueError, "仍有 watch / serve"):
                    self.run_removal()
        self.assertTrue(self.captures.exists())

    def test_legacy_monitor_index_lock_blocks_cleanup(self):
        index = ChatIndex(self.captures / "monitor/index.sqlite3")
        try:
            with self.assertRaisesRegex(ValueError, "索引正在使用"):
                self.run_removal()
        finally:
            index.close()
        self.assertTrue(self.captures.exists())

    def test_uv_detection_requires_current_environment(self):
        tools = Path(self.temp.name) / "tools"
        environment = tools / "codex-watch"
        result = SimpleNamespace(returncode=0, stdout=str(tools), stderr="")
        with patch("codex_watch.removal.shutil.which", return_value="/bin/uv"), \
             patch("codex_watch.removal.subprocess.run", return_value=result), \
             patch("codex_watch.removal.sys.prefix", str(environment)), \
             patch("codex_watch.removal.__file__", str(environment / "lib/codex_watch/removal.py")):
            self.assertIn("uninstall", uv_uninstaller())
            with patch("codex_watch.removal.sys.prefix", str(tools / "some-other-tool")):
                with self.assertRaisesRegex(ValueError, "拒绝卸载其他环境"):
                    uv_uninstaller()

    def test_cli_dry_run_and_confirmed_cleanup(self):
        command = [*cli_command(), "--data-dir", str(self.root), "purge"]
        preview = subprocess.run([*command, "--dry-run"], capture_output=True, text=True, check=True)
        self.assertIn(str(self.captures.resolve()), preview.stdout)
        self.assertTrue(self.captures.exists())
        subprocess.run([*command, "--yes"], capture_output=True, text=True, check=True)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
