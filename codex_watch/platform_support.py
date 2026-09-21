"""Platform-specific paths, file locking and child-process cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys


def data_directory(legacy_root=None):
    override = os.environ.get("CODEX_WATCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if legacy_root is not None:
        return Path(legacy_root).resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "codex-watch"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/codex-watch"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    if not base.is_absolute():
        base = Path.home() / ".local/share"
    return base / "codex-watch"


def restrict_file(fd):
    # Windows access controls are inherited from the current user's directory.
    if os.name != "nt":
        os.fchmod(fd, 0o600)


def lock_file(fd):
    if os.name == "nt":
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def child_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_child(child):
    if child.poll() is not None:
        return
    try:
        if os.name == "nt":
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.terminate()
        child.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        if child.poll() is None:
            child.kill()
        child.wait()


def display_command(command):
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def run_client(command, env):
    if os.name != "nt":
        os.execvpe(command[0], command, env)
    # Let the interactive child share the console; Ctrl+C reaches both processes.
    child = subprocess.Popen(command, env=env)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue
