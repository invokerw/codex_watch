"""Prevent data removal while a managed monitor or proxy is running."""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import tempfile

from codex_watch.platform_support import lock_file


@contextmanager
def data_locks(data_root, roles):
    identity = os.path.normcase(str(Path(data_root).resolve()))
    key = hashlib.sha256(identity.encode()).hexdigest()
    user_key = hashlib.sha256(str(Path.home().resolve()).encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / f"codex-watch-locks-{user_key}"
    directory.mkdir(mode=0o700, exist_ok=True)
    handles = []
    try:
        for role in sorted(roles):
            handle = os.open(directory / f"{key}.{role}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            handles.append(handle)
            try:
                lock_file(handle)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ValueError("此数据目录仍有 watch / serve 在运行，请先停止它们。") from exc
                raise
        yield
    finally:
        for handle in reversed(handles):
            os.close(handle)
