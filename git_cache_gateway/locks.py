from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


def safe_lock_name(key: str) -> str:
    return key.replace("/", "__").replace(":", "_").replace("@", "_") + ".lock"


@contextmanager
def file_lock(lockdir: Path, key: str):
    lockdir.mkdir(parents=True, exist_ok=True)
    lock_path = lockdir / safe_lock_name(key)
    with lock_path.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
