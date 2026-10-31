"""Tests for the cross-process sync lock (design S3)."""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING

from imap_mcp.adapters.flock import FileLock
from imap_mcp.ports import SyncLock
from lockholder import hold_lock

if TYPE_CHECKING:
    from pathlib import Path


class TestFileLock:
    def test_it_satisfies_the_port(self, tmp_path: Path) -> None:
        assert isinstance(FileLock(tmp_path / "sync.lock"), SyncLock)

    def test_a_second_process_cannot_take_a_held_lock(self, tmp_path: Path) -> None:
        path = tmp_path / "sync.lock"
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=hold_lock, args=(str(path), ready, release))
        holder.start()
        try:
            assert ready.wait(10)
            lock = FileLock(path)
            assert lock.try_acquire() is False
            assert lock.acquire(0.2) is False
        finally:
            release.set()
            holder.join(10)

        assert FileLock(path).try_acquire() is True

    def test_a_crashed_holder_leaves_no_stale_lock(self, tmp_path: Path) -> None:
        path = tmp_path / "sync.lock"
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=hold_lock, args=(str(path), ready, release))
        holder.start()
        assert ready.wait(10)

        holder.kill()
        holder.join(10)

        assert FileLock(path).try_acquire() is True
