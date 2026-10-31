"""A helper process that holds the sync lock. Spawned by the lock tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from imap_mcp.adapters.flock import FileLock

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event


def hold_lock(path: str, ready: Event, release: Event) -> None:
    """Take the lock, signal `ready`, then wait for `release`."""
    lock = FileLock(Path(path))
    if lock.try_acquire():
        ready.set()
    release.wait(10)
