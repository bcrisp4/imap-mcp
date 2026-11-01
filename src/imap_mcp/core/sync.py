"""The sync engine (design Appendix C).

One process at a time syncs, guarded by the sync lock. A sync started by a
tool call has a time budget. When the budget runs out, the engine stops between
batches and reports a partial sync, and the next sync continues from there.

No network call happens inside a database transaction: each batch is fetched
and parsed first, then written in one short transaction.
"""

from __future__ import annotations

import math
import traceback
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import structlog

from imap_mcp.domain.content.auth import DEFAULT_AUTH_SERVERS
from imap_mcp.domain.content.parse import MAX_TEXT_BYTES, header_identity, parse_message
from imap_mcp.domain.enums import FolderRole, SyncStateKind
from imap_mcp.domain.errors import (
    CredentialError,
    MailError,
    NotFoundError,
    UnexpectedResponseError,
)
from imap_mcp.domain.model import SyncReport, SyncState

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from datetime import datetime, timedelta

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
TEXT_CHUNK_WITH_BUDGET: Final = 10
"""Messages per text fetch when a time budget applies, so the engine can stop
inside a batch instead of after up to 50 MB of text."""
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
    skipped: int = 0
    unvisited: bool = False
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
        auth_servers: Sequence[str] = DEFAULT_AUTH_SERVERS,
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
            auth_servers: Domains whose `Authentication-Results` headers count.
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
        self._auth_servers = tuple(auth_servers)
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
            # Another process finished its sync while this one waited.
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
            return self._failed(previous, started, only_folder, str(exc))
        except NotFoundError:
            raise
        except Exception as exc:
            # A bug, or a message that no parser expects. Record the failure
            # so that tool calls within the interval read the index instead
            # of syncing again into the same error.
            # The traceback frames show where it failed. The exception message
            # is left out, because it can quote mail content.
            log.error("sync_failed", error_type=type(exc).__name__,
                      trace=traceback.format_tb(exc.__traceback__))  # fmt: skip
            error = f"Sync failed with an unexpected {type(exc).__name__}"
            return self._failed(previous, started, only_folder, error)

        # A skipped message keeps the sync partial, so the state never claims
        # an index that misses mail.
        complete = not run.interrupted and not run.skipped
        kind = SyncStateKind.COMPLETE if complete else SyncStateKind.PARTIAL
        remaining = None if run.unvisited else run.remaining + run.skipped
        # A single-folder sync says nothing about the other folders, so it
        # leaves the shared freshness state alone.
        if only_folder is None:
            if complete:
                self._index.delete_orphans()
                self._index.maintain(deadline, self._clock)
            self._index.save_sync_state(
                SyncState(
                    last_success=self._clock.now()
                    if complete
                    else previous.last_success,
                    last_attempt=started,
                    kind=kind,
                    error=None,
                    remaining=remaining,
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
            remaining=remaining,
            folders=tuple(run.folders),
        )

    def _failed(
        self,
        previous: SyncState,
        started: datetime,
        only_folder: str | None,
        error: str,
    ) -> SyncReport:
        if only_folder is None:
            self._index.save_sync_state(
                replace(previous, last_attempt=started, kind=SyncStateKind.FAILED,
                        error=error)
            )  # fmt: skip
        return SyncReport(kind=SyncStateKind.FAILED, error=error)

    def _sync_folders(
        self, source: MailSource, run: _Run, only_folder: str | None
    ) -> None:
        folders = [f for f in source.list_folders() if f.name not in self._exclude]
        if only_folder is None:
            self._index.remove_folders_except([f.name for f in folders])
        else:
            folders = [f for f in folders if f.name == only_folder]
            if not folders:
                msg = f"No folder named {only_folder!r}. Call list_folders for names."
                raise NotFoundError(msg)
        folders.sort(key=lambda f: (_ROLE_ORDER[f.role], f.name.casefold()))
        condstore = "CONDSTORE" in source.capabilities()
        for position, folder in enumerate(folders):
            if self._clock.monotonic() >= run.deadline:
                run.interrupted = True
                run.unvisited = True
                return
            self._sync_folder(source, folder, run, condstore=condstore,
                              force_flags=only_folder is not None)  # fmt: skip
            if run.interrupted:
                run.unvisited = position < len(folders) - 1
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
        skipped_before = run.skipped
        uidvalidity = source.examine(name)
        if self._index.bind_uidvalidity(name, folder.role, uidvalidity):
            log.warning("uidvalidity_changed", folder=name)
            stored = None

        server_uids = set(source.search_uids())
        local_uids = self._index.folder_uids(name)
        gone = local_uids - server_uids
        run.removed += self._index.remove_locations(name, gone)
        kept = local_uids - gone

        if kept and stored is None:
            # An earlier run fetched these but never finished the folder, so
            # no stored modseq covers changes made since. Fetch every flag.
            run.flags_updated += self._index.update_flags(
                name, source.fetch_flags(kept)
            )
        elif kept and stored is not None:
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
            batch = new[start : start + self._batch_size]
            self._sync_batch(source, name, batch, run)
            if run.interrupted:
                run.remaining += len(new) - start - len(batch)
                return
            if self._progress is not None:
                self._progress(name, min(start + self._batch_size, len(new)), len(new))

        # Saving the status would make the next sync skip this folder, and so
        # never try a skipped message again.
        if run.skipped == skipped_before:
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
            # CHANGEDSINCE filters on the server, so one closed range replaces
            # a long explicit UID set. UIDs in the range that the index does
            # not hold match no location and change nothing.
            span = range(min(uids), max(uids) + 1)
            changed = source.fetch_flags(span, changed_since=stored.highestmodseq)
            return self._index.update_flags(name, changed)
        if status.unseen != stored.unseen:
            # Without CONDSTORE, a flag change shows only as a new unread
            # count. A change to \Flagged alone waits for `sync_now(folder)`.
            return self._index.update_flags(name, source.fetch_flags(uids))
        return 0

    def _sync_batch(
        self, source: MailSource, name: str, uids: Sequence[int], run: _Run
    ) -> None:
        headers = self._fetch_headers(source, name, uids, run)
        identities = {h.uid: header_identity(h) for h in headers}
        known = self._index.known_identities(set(identities.values()))

        links = [
            (h.uid, h.flags, known[identities[h.uid]])
            for h in headers
            if identities[h.uid] in known
        ]
        fresh = [h for h in headers if identities[h.uid] not in known]
        parsed, skipped = self._fetch_and_parse(source, fresh, run.deadline)

        run.linked += self._index.link_locations(name, links) if links else 0
        if parsed:
            run.new_messages += self._index.add_messages(name, parsed)
        if skipped:
            run.interrupted = True
            run.remaining += skipped

    def _fetch_headers(
        self, source: MailSource, name: str, uids: Sequence[int], run: _Run
    ) -> list[FetchedHeader]:
        """Fetch headers, and skip any message the client cannot parse.

        One hostile message must not stop the sync of every message around it.
        If a batch fails to parse, fetch its messages one at a time until the
        deadline. A skipped message keeps the folder unsaved and the sync
        partial, so the next sync tries it again. If every message of a batch
        fails, the cause is not one message, so the error stands.
        """
        try:
            return list(source.fetch_headers(uids))
        except UnexpectedResponseError:
            if len(uids) == 1:
                log.warning("message_skipped", folder=name, uid=uids[0])
                run.skipped += 1
                return []
        headers: list[FetchedHeader] = []
        failed = 0
        for position, uid in enumerate(uids):
            if self._clock.monotonic() >= run.deadline:
                run.interrupted = True
                run.remaining += len(uids) - position
                break
            try:
                headers.extend(source.fetch_headers([uid]))
            except UnexpectedResponseError:
                log.warning("message_skipped", folder=name, uid=uid)
                failed += 1
        if failed and failed == len(uids):
            msg = f"IMAP UID FETCH failed for every message of a batch in {name!r}"
            raise UnexpectedResponseError(msg)
        run.skipped += failed
        return headers

    def _fetch_and_parse(
        self, source: MailSource, headers: Sequence[FetchedHeader], deadline: float
    ) -> tuple[list[tuple[int, frozenset[str], ParsedMessage]], int]:
        """Fetch and parse new messages until the deadline.

        Returns:
            The parsed messages, and the number left unfetched because the
            deadline passed. Those stay out of the index, so the next sync
            fetches them.
        """
        groups: dict[tuple[str, ...], list[tuple[FetchedHeader, MessageStructure]]] = {}
        whole: list[FetchedHeader] = []
        for header in headers:
            if header.structure is None:
                whole.append(header)
            else:
                key = tuple(p.part for p in header.structure.text_parts)
                groups.setdefault(key, []).append((header, header.structure))

        chunk = len(headers) or 1
        if math.isfinite(deadline):
            chunk = TEXT_CHUNK_WITH_BUDGET
        work: list[tuple[list[FetchedHeader], MessageStructure | None]] = []
        for members in groups.values():
            group = [header for header, _ in members]
            for i in range(0, len(group), chunk):
                work.append((group[i : i + chunk], members[0][1]))
        work.extend(([header], None) for header in whole)

        out: list[tuple[int, frozenset[str], ParsedMessage]] = []
        done = 0
        for part, structure in work:
            if self._clock.monotonic() >= deadline:
                return out, len(headers) - done
            done += len(part)
            if structure is None:
                (header,) = part
                data = source.fetch_whole(header.uid, self._max_text_bytes)
                parsed = parse_message(
                    header,
                    None,
                    whole=data,
                    max_bytes=self._max_text_bytes,
                    auth_servers=self._auth_servers,
                )
                out.append((header.uid, header.flags, parsed))
                continue
            texts = (
                source.fetch_text([h.uid for h in part], structure.text_parts,
                                  self._max_text_bytes)
                if structure.text_parts
                else {}
            )  # fmt: skip
            for header in part:
                parsed = parse_message(
                    header,
                    texts.get(header.uid, {}),
                    max_bytes=self._max_text_bytes,
                    auth_servers=self._auth_servers,
                )
                out.append((header.uid, header.flags, parsed))
        return out, 0
