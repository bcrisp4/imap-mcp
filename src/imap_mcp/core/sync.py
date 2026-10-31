"""The sync engine (design Appendix C).

One process at a time syncs, guarded by the sync lock. A sync started by a
tool call has a time budget. When the budget runs out, the engine stops between
batches and reports a partial sync, and the next sync continues from there.

No network call happens inside a database transaction: each batch is fetched
and parsed first, then written in one short transaction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import structlog

from imap_mcp.domain.content.parse import MAX_TEXT_BYTES, parse_message
from imap_mcp.domain.enums import FolderRole, SyncStateKind
from imap_mcp.domain.errors import CredentialError, MailError
from imap_mcp.domain.model import SyncReport, SyncState

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from datetime import timedelta

    from imap_mcp.domain.model import (
        FetchedHeader,
        Folder,
        FolderStatus,
        MessageStructure,
        ParsedMessage,
    )
    from imap_mcp.ports import Clock, Index, MailSource, MailSourceFactory, SyncLock

log = structlog.get_logger(__name__)

BATCH_SIZE: Final = 50
_ROLE_ORDER: Final = {
    FolderRole.INBOX: 0,
    FolderRole.SENT: 1,
    FolderRole.DRAFTS: 2,
    FolderRole.ARCHIVE: 3,
    FolderRole.OTHER: 4,
    FolderRole.JUNK: 5,
    FolderRole.TRASH: 6,
}


@dataclass(slots=True)
class _Run:
    """Counters for one sync run."""

    deadline: float
    new_messages: int = 0
    linked: int = 0
    removed: int = 0
    flags_updated: int = 0
    remaining: int = 0
    interrupted: bool = False
    folders: list[str] = field(default_factory=list[str])


class SyncService:
    """Keeps the index in step with the IMAP server."""

    def __init__(
        self,
        *,
        index: Index,
        sources: MailSourceFactory,
        lock: SyncLock,
        clock: Clock,
        interval: timedelta,
        budget_seconds: float,
        exclude_folders: Collection[str] = (),
        batch_size: int = BATCH_SIZE,
        max_text_bytes: int = MAX_TEXT_BYTES,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Create the service.

        Args:
            index: The local index.
            sources: Opens IMAP sessions.
            lock: The cross-process sync lock.
            clock: The time source.
            interval: How old the last sync can be before a tool call syncs.
            budget_seconds: The time limit of a sync that a tool call starts.
            exclude_folders: Folders never to sync.
            batch_size: Messages fetched and written together.
            max_text_bytes: The text fetch cap for each message.
            progress: Called with (folder, done, total) during a sync.
        """
        self._index = index
        self._sources = sources
        self._lock = lock
        self._clock = clock
        self._interval = interval
        self._budget = budget_seconds
        self._exclude = frozenset(exclude_folders)
        self._batch_size = batch_size
        self._max_text_bytes = max_text_bytes
        self._progress = progress

    # -- entry points -----------------------------------------------------------

    def state(self) -> SyncState:
        """Return the shared sync state, marked if another process is syncing."""
        state = self._index.sync_state()
        if self._lock.try_acquire():
            self._lock.release()
            return state
        return replace(state, kind=SyncStateKind.IN_PROGRESS_ELSEWHERE)

    def ensure_fresh(self) -> SyncState:
        """Sync first if the last attempt is older than the interval.

        Returns:
            The sync state after any sync. If another process holds the lock,
            the state says `in_progress_elsewhere` and no sync runs here.
        """
        if self._is_fresh(self._index.sync_state()):
            return self._index.sync_state()
        if not self._lock.try_acquire():
            return replace(
                self._index.sync_state(), kind=SyncStateKind.IN_PROGRESS_ELSEWHERE
            )
        try:
            # Another process can finish a sync between the first check and
            # the lock. Check again so the work runs once.
            if not self._is_fresh(self._index.sync_state()):
                self._run(self._clock.monotonic() + self._budget)
        finally:
            self._lock.release()
        return self._index.sync_state()

    def sync_now(self, folder: str | None = None) -> SyncReport:
        """Sync now within the time budget, whatever the interval says.

        If another process is syncing, wait for it for up to the budget and
        report its outcome. A second sync never starts.

        Args:
            folder: Limit the sync to one folder. Its flags are all refetched,
                which also catches flag changes that `STATUS` does not show.

        Returns:
            What the sync did.
        """
        if self._lock.try_acquire():
            try:
                return self._run(
                    self._clock.monotonic() + self._budget, only_folder=folder
                )
            finally:
                self._lock.release()
        if self._lock.acquire(self._budget):
            self._lock.release()
            state = self._index.sync_state()
            return SyncReport(
                kind=state.kind, remaining=state.remaining, error=state.error
            )
        return SyncReport(kind=SyncStateKind.IN_PROGRESS_ELSEWHERE)

    def full_sync(self) -> SyncReport:
        """Run a sync with no time limit. The CLI uses this for the backfill.

        Returns:
            What the sync did, or `in_progress_elsewhere` if another process
            holds the lock.
        """
        if not self._lock.try_acquire():
            return SyncReport(kind=SyncStateKind.IN_PROGRESS_ELSEWHERE)
        try:
            return self._run(math.inf)
        finally:
            self._lock.release()

    # -- the algorithm -------------------------------------------------------------

    def _is_fresh(self, state: SyncState) -> bool:
        # Measured from the last attempt, whatever its outcome, so a failing
        # server or a long backfill does not add a sync to every tool call.
        if state.last_attempt is None:
            return False
        return self._clock.now() - state.last_attempt < self._interval

    def _run(self, deadline: float, only_folder: str | None = None) -> SyncReport:
        started = self._clock.now()
        previous = self._index.sync_state()
        run = _Run(deadline=deadline)
        try:
            with self._sources.open() as source:
                self._sync_folders(source, run, only_folder)
        except (MailError, CredentialError) as exc:
            log.error("sync_failed", error=str(exc))
            self._index.save_sync_state(
                replace(previous, last_attempt=started, kind=SyncStateKind.FAILED,
                        error=str(exc))
            )  # fmt: skip
            return SyncReport(kind=SyncStateKind.FAILED, error=str(exc))

        complete = not run.interrupted
        if complete and only_folder is None:
            self._index.delete_orphans()
            self._index.maintain(deadline, self._clock)
        kind = SyncStateKind.COMPLETE if complete else SyncStateKind.PARTIAL
        self._index.save_sync_state(
            SyncState(
                last_success=self._clock.now() if complete else previous.last_success,
                last_attempt=started,
                kind=kind,
                error=None,
                remaining=run.remaining,
            )
        )
        log.info(
            "sync_finished", kind=kind.value, new=run.new_messages, linked=run.linked,
            removed=run.removed, flags=run.flags_updated, remaining=run.remaining,
        )  # fmt: skip
        return SyncReport(
            kind=kind,
            new_messages=run.new_messages,
            linked=run.linked,
            removed=run.removed,
            flags_updated=run.flags_updated,
            remaining=run.remaining,
            folders=tuple(run.folders),
        )

    def _sync_folders(
        self, source: MailSource, run: _Run, only_folder: str | None
    ) -> None:
        folders = [f for f in source.list_folders() if f.name not in self._exclude]
        if only_folder is None:
            self._index.remove_folders_except([f.name for f in folders])
        else:
            folders = [f for f in folders if f.name == only_folder]
        folders.sort(key=lambda f: (_ROLE_ORDER[f.role], f.name.casefold()))
        condstore = "CONDSTORE" in source.capabilities()
        for folder in folders:
            if self._clock.monotonic() >= run.deadline:
                run.interrupted = True
                return
            self._sync_folder(source, folder, run, condstore=condstore,
                              force_flags=only_folder is not None)  # fmt: skip
            if run.interrupted:
                return

    def _sync_folder(
        self,
        source: MailSource,
        folder: Folder,
        run: _Run,
        *,
        condstore: bool,
        force_flags: bool,
    ) -> None:
        name = folder.name
        status = source.status(name)
        stored = self._index.folder_status(name)
        if stored == status and not force_flags:
            return
        run.folders.append(name)
        uidvalidity = source.examine(name)
        if self._index.bind_uidvalidity(name, folder.role, uidvalidity):
            log.warning("uidvalidity_changed", folder=name)
            stored = None

        server_uids = set(source.search_uids())
        local_uids = self._index.folder_uids(name)
        gone = local_uids - server_uids
        run.removed += self._index.remove_locations(name, gone)
        kept = local_uids - gone

        if kept and stored is not None:
            run.flags_updated += self._refresh_flags(
                source, name, kept, status, stored, condstore=condstore,
                force=force_flags,
            )  # fmt: skip

        new = sorted(server_uids - local_uids, reverse=True)
        for start in range(0, len(new), self._batch_size):
            if self._clock.monotonic() >= run.deadline:
                run.interrupted = True
                run.remaining += len(new) - start
                return
            self._sync_batch(source, name, new[start : start + self._batch_size], run)
            if self._progress is not None:
                self._progress(name, min(start + self._batch_size, len(new)), len(new))

        self._index.save_folder(name, folder.role, status)

    def _refresh_flags(
        self,
        source: MailSource,
        name: str,
        uids: set[int],
        status: FolderStatus,
        stored: FolderStatus,
        *,
        condstore: bool,
        force: bool,
    ) -> int:
        if force:
            return self._index.update_flags(name, source.fetch_flags(uids))
        if (
            condstore
            and stored.highestmodseq is not None
            and status.highestmodseq is not None
        ):
            if status.highestmodseq == stored.highestmodseq:
                return 0
            changed = source.fetch_flags(uids, changed_since=stored.highestmodseq)
            return self._index.update_flags(name, changed)
        if status.unseen != stored.unseen:
            # Without CONDSTORE, a flag change shows only as a new unread
            # count. A change to \Flagged alone waits for `sync_now(folder)`.
            return self._index.update_flags(name, source.fetch_flags(uids))
        return 0

    def _sync_batch(
        self, source: MailSource, name: str, uids: Sequence[int], run: _Run
    ) -> None:
        headers = source.fetch_headers(uids)
        first_pass = {h.uid: parse_message(h, None) for h in headers}
        known = self._index.known_identities({p.identity for p in first_pass.values()})

        links = [
            (h.uid, h.flags, known[first_pass[h.uid].identity])
            for h in headers
            if first_pass[h.uid].identity in known
        ]
        fresh = [h for h in headers if first_pass[h.uid].identity not in known]
        parsed = self._fetch_and_parse(source, fresh)

        run.linked += self._index.link_locations(name, links) if links else 0
        if parsed:
            run.new_messages += self._index.add_messages(name, parsed)

    def _fetch_and_parse(
        self, source: MailSource, headers: Sequence[FetchedHeader]
    ) -> list[tuple[int, frozenset[str], ParsedMessage]]:
        groups: dict[tuple[str, ...], list[tuple[FetchedHeader, MessageStructure]]] = {}
        whole: list[FetchedHeader] = []
        for header in headers:
            if header.structure is None:
                whole.append(header)
            else:
                key = tuple(p.part for p in header.structure.text_parts)
                groups.setdefault(key, []).append((header, header.structure))

        out: list[tuple[int, frozenset[str], ParsedMessage]] = []
        for members in groups.values():
            group = [header for header, _ in members]
            structure = members[0][1]
            texts = (
                source.fetch_text([h.uid for h in group], structure.text_parts,
                                  self._max_text_bytes)
                if structure.text_parts
                else {}
            )  # fmt: skip
            for header in group:
                parsed = parse_message(
                    header, texts.get(header.uid, {}), max_bytes=self._max_text_bytes
                )
                out.append((header.uid, header.flags, parsed))
        for header in whole:
            data = source.fetch_whole(header.uid, self._max_text_bytes)
            parsed = parse_message(
                header, None, whole=data, max_bytes=self._max_text_bytes
            )
            out.append((header.uid, header.flags, parsed))
        return out
