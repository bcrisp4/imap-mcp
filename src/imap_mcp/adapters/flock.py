"""The cross-process sync lock. Implements `SyncLock`.

It uses `fcntl.flock` on a file next to the database. The kernel releases the
lock when the holding process exits or crashes, so a stale lock cannot exist.
`flock` needs a local file system, which the database already requires.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

_POLL_SECONDS: Final = 0.1
_FILE_MODE: Final = 0o600


class FileLock:
    """An exclusive, non-blocking `flock` on one file."""

    def __init__(self, path: Path) -> None:
        """Create the lock. The file is created on first use.

        Args:
            path: The lock file.
        """
        self._path = path
        self._fd: int | None = None
        self._held = False
        self._mutex = threading.Lock()

    def try_acquire(self) -> bool:
        """Take the lock without a wait. Return False if another holds it."""
        with self._mutex:
            if self._held:
                return False
            if self._fd is None:
                self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, _FILE_MODE)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self._held = True
            return True

    def acquire(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the lock. Return True if taken."""
        deadline = time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)

    def release(self) -> None:
        """Release the lock. Releasing a lock not held does nothing."""
        with self._mutex:
            if self._held and self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._held = False

    def close(self) -> None:
        """Release the lock and close the file."""
        self.release()
        with self._mutex:
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None
