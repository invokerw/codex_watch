"""Explicit data cleanup and self-uninstall for uv-managed installations."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys

from codex_watch.lifecycle import data_locks
from codex_watch.platform_support import lock_file


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def removal_targets(data_root):
    root = Path(data_root).resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("数据目录不能是磁盘根目录或用户主目录；请指定 codex-watch 专用数据目录。")
    targets = [root / "captures", root / ".mitmproxy"]
    for target in targets:
        if linked(target):
            raise ValueError(f"清理路径是符号链接或联接点，请先手动处理：{target}")
        if not target.exists():
            continue
        if not target.is_dir():
            raise ValueError(f"预期数据目录，但发现普通文件：{target}")
        for base, dirs, files in os.walk(target, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                if linked(path):
                    raise ValueError(f"数据目录中存在符号链接或联接点，请先手动处理：{path}")
    return targets


@contextmanager
def idle_index(data_root):
    # Also recognize a monitor from releases predating the lifecycle locks.
    path = Path(data_root) / "captures/monitor/index.sqlite3.lock"
    fd = None
    try:
        if path.exists():
            fd = os.open(path, os.O_RDWR)
            try:
                lock_file(fd)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ValueError("聊天索引正在使用，请先停止 watch。") from exc
                raise
        yield
    finally:
        if fd is not None:
            os.close(fd)


def uv_uninstaller():
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("未找到 uv。可先运行 purge 清理数据，再使用原安装方式卸载程序。")
    result = subprocess.run([uv, "tool", "dir", "--offline", "--no-config"], capture_output=True,
                            text=True, timeout=10)
    if result.returncode or not result.stdout.strip():
        raise ValueError("无法确定 uv 工具目录；尚未删除数据。")
    environment = Path(result.stdout.strip()) / "codex-watch"
    prefix = Path(sys.prefix).resolve()
    if environment.resolve() != prefix or not Path(__file__).resolve().is_relative_to(prefix):
        raise ValueError("当前程序不是该 uv 管理的 codex-watch 安装，拒绝卸载其他环境。"
                         "可先运行 purge 清理数据，再使用原安装方式卸载程序。")
    return [uv, "tool", "uninstall", "codex-watch", "--offline", "--no-config"]


def run_removal(args, data_root):
    uninstall = args.command == "uninstall"
    keep = uninstall and args.keep_data
    targets = [] if keep else removal_targets(data_root)
    command = None
    installation_error = None
    if uninstall:
        try:
            command = uv_uninstaller()
        except ValueError as exc:
            installation_error = str(exc)
    if targets:
        print("将删除以下目录中的抓包、聊天索引、报告和证书：")
        for target in targets:
            print(f"  {target}" + ("（不存在）" if not target.exists() else ""))
        print("仅清理以上目录；单独指定到其他位置的 --file / --index / --confdir 不会被扫描删除。")
    else:
        print("保留聊天数据和证书。")
    if uninstall:
        print("程序：卸载当前 uv 工具环境中的 codex-watch。")
        if installation_error:
            print(installation_error)
    if args.dry_run:
        print("预览结束，未删除任何内容。")
        return 0
    if installation_error:
        raise ValueError(installation_error)
    with data_locks(data_root, ("watch", "serve")):
        # Check before confirmation; release the old index lock before removing it
        # because Windows does not permit deleting an open file.
        with idle_index(data_root):
            pass
        if not args.yes:
            try:
                answer = input("确认执行请输入 DELETE，其他输入取消：")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer != "DELETE":
                print("已取消，未删除任何内容。")
                return 0
        if not keep:
            # Recheck after a potentially long interactive confirmation.
            with idle_index(data_root):
                pass
            targets = removal_targets(data_root)
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
                    print(f"已删除：{target}")
            try:
                Path(data_root).rmdir()  # Only remove the root if it is now empty.
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise
            print("当前数据目录的抓包、索引、报告和证书已清理。")
        if command:
            # The current directory may itself have just been removed.
            result = subprocess.run(command, cwd=Path(sys.prefix).parent, capture_output=True, text=True)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                state = "数据已清理，但程序卸载失败" if not keep else "程序卸载失败，数据已保留"
                raise ValueError(f"{state}：{detail}")
            print("codex-watch 已卸载。")
    return 0
