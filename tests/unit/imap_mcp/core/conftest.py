"""Fixtures for core tests: a real index, the fake server and fake clock."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from fakes import FakeClock, FakeCredentialSource, FakeLock
from imap_mcp.adapters.imap.session import ImapMailSourceFactory
from imap_mcp.adapters.sqlite.connection import open_database
from imap_mcp.adapters.sqlite.index import SqliteIndex
from imap_mcp.adapters.sqlite.schema import migrate
from imap_mcp.core.sync import SyncService
from imapserver.server import PASSWORD, USERNAME

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from imapserver.server import FakeImapServer


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SqliteIndex]:
    conn = open_database(tmp_path / "imap-mcp.db")
    migrate(conn)
    yield SqliteIndex(conn)
    conn.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def lock() -> FakeLock:
    return FakeLock()


@pytest.fixture
def source_factory(
    imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
) -> Callable[..., ImapMailSourceFactory]:
    def make(password: str = PASSWORD) -> ImapMailSourceFactory:
        return ImapMailSourceFactory(
            host="localhost",
            port=imap_server.port,
            username=USERNAME,
            credentials=FakeCredentialSource(password),
            ssl_context=client_ssl_context,
        )

    return make


@pytest.fixture
def make_sync(
    index: SqliteIndex,
    clock: FakeClock,
    lock: FakeLock,
    source_factory: Callable[..., ImapMailSourceFactory],
) -> Callable[..., SyncService]:
    def make(**overrides: object) -> SyncService:
        options: dict[str, object] = {
            "index": index,
            "sources": source_factory(),
            "lock": lock,
            "clock": clock,
            "interval": timedelta(minutes=15),
            "budget_seconds": 20.0,
        }
        options.update(overrides)
        return SyncService(**options)  # type: ignore[arg-type]

    return make
