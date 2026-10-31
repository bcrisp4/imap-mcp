"""Ports: the interfaces between the core and the outside world.

The core depends only on these Protocols. Adapters in `imap_mcp.adapters`
implement them, and `imap_mcp.bootstrap` wires the two together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime
    from pathlib import Path

    from imap_mcp.domain.enums import FolderRole
    from imap_mcp.domain.model import (
        FetchedHeader,
        Folder,
        FolderStatus,
        FolderSummary,
        ParsedMessage,
        SearchPage,
        SearchQuery,
        StoredMessage,
        SyncState,
        TextPart,
    )


@runtime_checkable
class MailSource(Protocol):
    """One open, logged-in IMAP session.

    Every method is read-only except `append_draft`, which writes only to the
    Drafts folder. A folder-scoped method acts on the folder that the last
    `examine` call opened.
    """

    def capabilities(self) -> frozenset[str]:
        """Return the post-login capabilities, upper case."""
        ...

    def list_folders(self) -> list[Folder]:
        """Return every folder, with its role."""
        ...

    def status(self, folder: str) -> FolderStatus:
        """Return the STATUS values of a folder without opening it."""
        ...

    def examine(self, folder: str) -> FolderStatus:
        """Open a folder read-only and return its status."""
        ...

    def search_uids(self) -> list[int]:
        """Return every UID in the open folder."""
        ...

    def search_message_id(self, message_id: str) -> list[int]:
        """Return the UIDs in the open folder with this `Message-ID`."""
        ...

    def fetch_headers(self, uids: Collection[int]) -> list[FetchedHeader]:
        """Return flags, dates, sizes, headers and structure for these UIDs."""
        ...

    def fetch_text(
        self, uid: int, parts: Sequence[TextPart], max_bytes: int
    ) -> dict[str, bytes]:
        """Return the encoded bytes of text parts, each capped at `max_bytes`."""
        ...

    def fetch_whole(self, uid: int, max_bytes: int) -> bytes:
        """Return the first `max_bytes` of the whole message."""
        ...

    def fetch_flags(
        self, uids: Collection[int], changed_since: int | None = None
    ) -> dict[int, frozenset[str]]:
        """Return flags for these UIDs, optionally only those changed since."""
        ...

    def fetch_part(self, uid: int, part: str) -> bytes:
        """Return the encoded bytes of one MIME part."""
        ...

    def append_draft(self, folder: str, message: bytes) -> None:
        """Store a message in the Drafts folder with the `\\Draft` flag."""
        ...


@runtime_checkable
class MailSourceFactory(Protocol):
    """Opens IMAP sessions."""

    def open(self) -> AbstractContextManager[MailSource]:
        """Connect and log in. The context manager logs out on exit."""
        ...


@runtime_checkable
class CredentialSource(Protocol):
    """Supplies the IMAP password."""

    def password(self) -> str:
        """Return the IMAP password."""
        ...


@runtime_checkable
class SyncLock(Protocol):
    """A lock that allows one sync at a time across processes."""

    def try_acquire(self) -> bool:
        """Take the lock without a wait. Return False if another holds it."""
        ...

    def acquire(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the lock. Return True if taken."""
        ...

    def release(self) -> None:
        """Release the lock. Releasing a lock not held does nothing."""
        ...


@runtime_checkable
class Clock(Protocol):
    """The time source. Tests replace it."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic time in seconds, for time budgets."""
        ...


@runtime_checkable
class FileSink(Protocol):
    """Writes downloaded attachments."""

    def save(self, filename: str, data: bytes) -> Path:
        """Write a new file and return its path. Never overwrite a file."""
        ...


@runtime_checkable
class Index(Protocol):
    """The local message index.

    Each write method runs one short transaction. No caller holds a
    transaction open across a network call.
    """

    def folder_status(self, folder: str) -> FolderStatus | None:
        """Return the status stored after the last complete folder sync."""
        ...

    def save_folder(
        self, folder: str, role: FolderRole, status: FolderStatus | None
    ) -> None:
        """Record a folder and its status. None marks the folder for resync."""
        ...

    def remove_folders_except(self, names: Collection[str]) -> int:
        """Delete folders, and their locations, not in `names`."""
        ...

    def folder_uids(self, folder: str) -> set[int]:
        """Return the UIDs the index holds for a folder."""
        ...

    def remove_locations(self, folder: str, uids: Collection[int]) -> int:
        """Delete locations that the server no longer has."""
        ...

    def clear_folder(self, folder: str) -> None:
        """Delete every location of a folder and mark it for resync."""
        ...

    def known_identities(self, identities: Collection[str]) -> dict[str, int]:
        """Return the message IDs of identities already in the index."""
        ...

    def add_messages(
        self,
        folder: str,
        items: Sequence[tuple[int, frozenset[str], ParsedMessage]],
    ) -> int:
        """Store new messages with their locations. Return the number added."""
        ...

    def link_locations(
        self, folder: str, items: Sequence[tuple[int, frozenset[str], int]]
    ) -> int:
        """Add locations, as (uid, flags, message id), for stored messages."""
        ...

    def update_flags(self, folder: str, flags: Mapping[int, frozenset[str]]) -> int:
        """Replace the flags of locations. Return the number changed."""
        ...

    def delete_orphans(self) -> int:
        """Delete messages that no folder holds."""
        ...

    def sync_state(self) -> SyncState:
        """Return the shared sync state."""
        ...

    def save_sync_state(self, state: SyncState) -> None:
        """Replace the shared sync state."""
        ...

    def search(
        self, query: SearchQuery, excluded_folders: Collection[str]
    ) -> SearchPage:
        """Run a search. Skip `excluded_folders` unless the query names one."""
        ...

    def get_message(self, message_id: int) -> StoredMessage | None:
        """Return one message, or None."""
        ...

    def locations(self, message_id: int) -> list[tuple[str, int]]:
        """Return the (folder, uid) pairs of a message."""
        ...

    def list_folders(self) -> list[FolderSummary]:
        """Return every folder with its counts."""
        ...

    def folders_with_role(self, roles: Collection[FolderRole]) -> list[str]:
        """Return the names of folders with any of these roles."""
        ...

    def known_addresses(self, addresses: Collection[str]) -> set[str]:
        """Return the addresses, lower case, that I sent mail to."""
        ...

    def maintain(self, deadline: float, clock: Clock) -> int:
        """Free unused pages until `deadline` (monotonic). Return pages freed."""
        ...
