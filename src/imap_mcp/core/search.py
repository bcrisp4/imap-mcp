"""Read operations: search, read one message, list folders.

Each operation first asks the sync service to bring the index up to date (at
most once per interval), then answers from the index.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

from imap_mcp.domain.errors import InvalidRequestError, NotFoundError
from imap_mcp.domain.folders import EXCLUDED_FROM_SEARCH

if TYPE_CHECKING:
    from imap_mcp.core.sync import SyncService
    from imap_mcp.domain.model import (
        FolderSummary,
        SearchPage,
        SearchQuery,
        StoredMessage,
        SyncState,
    )
    from imap_mcp.ports import Index

MAX_LIMIT: Final = 50


class ReadService:
    """Answers read requests from the index."""

    def __init__(self, *, index: Index, sync: SyncService) -> None:
        """Create the service.

        Args:
            index: The local index.
            sync: Keeps the index fresh.
        """
        self._index = index
        self._sync = sync

    def search(self, query: SearchQuery) -> tuple[SearchPage, SyncState]:
        """Search the index.

        Junk and Trash folders are skipped unless the query names one.

        Args:
            query: The search.

        Returns:
            The page of results and the sync state.

        Raises:
            InvalidRequestError: If the limit or offset is out of range.
        """
        if not 1 <= query.limit <= MAX_LIMIT:
            msg = f"limit must be between 1 and {MAX_LIMIT}."
            raise InvalidRequestError(msg)
        if query.offset < 0:
            msg = "offset must not be negative."
            raise InvalidRequestError(msg)
        state = self._sync.ensure_fresh()
        if query.text is not None and not query.text.strip():
            query = replace(query, text=None)
        excluded = self._index.folders_with_role(EXCLUDED_FROM_SEARCH)
        return self._index.search(query, excluded), state

    def get_message(self, message_id: int) -> tuple[StoredMessage, SyncState]:
        """Return one message.

        Raises:
            NotFoundError: If the index has no message with this ID.
        """
        state = self._sync.ensure_fresh()
        message = self._index.get_message(message_id)
        if message is None:
            msg = f"No message with id {message_id}. Search again for a current id."
            raise NotFoundError(msg)
        return message, state

    def list_folders(self) -> tuple[list[FolderSummary], SyncState]:
        """Return every folder with its counts."""
        state = self._sync.ensure_fresh()
        return self._index.list_folders(), state
