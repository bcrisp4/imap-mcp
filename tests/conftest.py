"""Shared fixtures: the TLS test CA and the fake IMAP server."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog
import trustme

from imapserver.server import PASSWORD, USERNAME, FakeImapServer, Quirks

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the real config and reset structlog."""
    monkeypatch.delenv("IMAP_MCP_CONFIG", raising=False)
    structlog.reset_defaults()


@pytest.fixture(scope="session")
def test_ca() -> trustme.CA:
    """A certificate authority that exists only for this test run."""
    return trustme.CA()


@pytest.fixture(scope="session")
def server_ssl_context(test_ca: trustme.CA) -> ssl.SSLContext:
    """The fake server's TLS context, with a certificate for localhost."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    # trustme is partly untyped, so its objects are used through Any.
    cast("Any", test_ca.issue_cert("localhost", "127.0.0.1")).configure_cert(context)
    return context


@pytest.fixture(scope="session")
def client_ssl_context(test_ca: trustme.CA) -> ssl.SSLContext:
    """A client TLS context that trusts only the test CA."""
    context = ssl.create_default_context()
    cast("Any", test_ca).configure_trust(context)
    return context


@pytest.fixture
def imap_server_factory(
    server_ssl_context: ssl.SSLContext,
) -> Iterator[Callable[..., FakeImapServer]]:
    """Start fake servers. Each one fails the test on a forbidden command."""
    servers: list[FakeImapServer] = []

    def start(quirks: Quirks | None = None) -> FakeImapServer:
        server = FakeImapServer(server_ssl_context, quirks)
        server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()
        server.assert_only_allowed()


@pytest.fixture
def imap_server(imap_server_factory: Callable[..., FakeImapServer]) -> FakeImapServer:
    """A fake server with INBOX, Archive, Drafts, Sent Messages and Junk."""
    server = imap_server_factory()
    for name in ("INBOX", "Archive", "Drafts", "Sent Messages", "Junk"):
        server.add_mailbox(name)
    return server


__all__ = ["PASSWORD", "USERNAME"]
