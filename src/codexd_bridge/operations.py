"""Serialize idle-session continuation across independent bridge workers."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .transport import BridgeError


@contextmanager
def continuing(home: Path, thread_id: str):
    # Per-home locks also cover two registries or aliases targeting the same daemon.
    directory = home / ".codexd-bridge-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise BridgeError("Session operation lock directory must not be a symbolic link")
    name = hashlib.sha256(thread_id.encode()).hexdigest() + ".lock"
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BridgeError("Session operation lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("Another bridge request is continuing this session; read its state before retrying") from None
        yield
    finally:
        os.close(fd)
