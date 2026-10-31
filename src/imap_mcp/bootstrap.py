"""The composition root: build the adapters from the config and wire them into
the services. This is the only module that names concrete adapters.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from imap_mcp.adapters.clock import SystemClock
from imap_mcp.adapters.filesystem import DownloadDirectory
from imap_mcp.adapters.flock import FileLock
from imap_mcp.adapters.imap.session import ImapMailSourceFactory
from imap_mcp.adapters.keychain import KeychainCredentialSource
from imap_mcp.adapters.sqlite.connection import open_database
from imap_mcp.adapters.sqlite.index import SqliteIndex
from imap_mcp.adapters.sqlite.schema import migrate, needs_migration
from imap_mcp.core.attachments import AttachmentService
from imap_mcp.core.drafts import DraftService
from imap_mcp.core.search import ReadService
from imap_mcp.core.services import Services
from imap_mcp.core.sync import SyncService
from imap_mcp.domain.errors import ImapMcpError

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable
    from pathlib import Path

    from imap_mcp.config import Config
    from imap_mcp.ports import Clock, CredentialSource

LOCK_FILE: Final = "sync.lock"
MIGRATION_WAIT_SECONDS: Final = 30.0


def lock_path(config: Config) -> Path:
    """Return the sync lock file path, next to the database."""
    return config.db_path.with_name(LOCK_FILE)


def open_index(config: Config, lock: FileLock) -> SqliteIndex:
    """Open the database, and migrate it under the sync lock if it is old.

    The lock is taken only when a migration is pending. A sync can hold the
    lock for hours during a backfill, and a current schema needs no lock.

    Raises:
        ImapMcpError: If a migration is pending and another process holds the
            lock for too long.
    """
    conn = open_database(config.db_path)
    if not needs_migration(conn):
        return SqliteIndex(conn)
    if not lock.acquire(MIGRATION_WAIT_SECONDS):
        msg = "Another imap-mcp process holds the sync lock. Try again later."
        raise ImapMcpError(msg)
    try:
        migrate(conn)
    finally:
        lock.release()
    return SqliteIndex(conn)


def build_services(
    config: Config,
    *,
    credentials: CredentialSource | None = None,
    ssl_context: ssl.SSLContext | None = None,
    clock: Clock | None = None,
    quarantine: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> Services:
    """Build every service from the config.

    Args:
        config: The validated config.
        credentials: Replaces the Keychain. Only tests pass one.
        ssl_context: Replaces the default TLS context. Only tests pass one.
        clock: Replaces the system clock. Only tests pass one.
        quarantine: Set the quarantine attribute on downloads.
        progress: Receives (folder, done, total) during a sync.

    Returns:
        The services.
    """
    lock = FileLock(lock_path(config))
    index = open_index(config, lock)
    clock = clock or SystemClock()
    sources = build_sources(config, credentials, ssl_context)
    sync = SyncService(
        index=index,
        sources=sources,
        lock=lock,
        clock=clock,
        interval=timedelta(minutes=config.sync_interval_minutes),
        budget_seconds=float(config.sync_budget_seconds),
        exclude_folders=config.exclude_folders,
        auth_servers=config.auth_servers,
        progress=progress,
    )
    return Services(
        index=index,
        clock=clock,
        sync=sync,
        read=ReadService(index=index, sync=sync),
        attachments=AttachmentService(
            index=index,
            sources=sources,
            sink=DownloadDirectory(config.download_dir, quarantine=quarantine),
        ),
        drafts=DraftService(
            index=index,
            sources=sources,
            clock=clock,
            from_address=config.from_address,
        ),
    )


def build_sources(
    config: Config,
    credentials: CredentialSource | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> ImapMailSourceFactory:
    """Build the IMAP session factory. `imap-mcp probe` uses it on its own.

    Args:
        config: The validated config.
        credentials: Replaces the Keychain. Only tests pass one.
        ssl_context: Replaces the default TLS context. Only tests pass one.

    Returns:
        The factory.
    """
    return ImapMailSourceFactory(
        host=config.host,
        port=config.port,
        username=config.username,
        credentials=credentials or KeychainCredentialSource(config.username),
        drafts_folder=config.drafts_folder,
        ssl_context=ssl_context,
    )
