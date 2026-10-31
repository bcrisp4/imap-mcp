"""Fixtures that wire the real services to the fake server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fakes import FakeClock, FakeCredentialSource
from imap_mcp.bootstrap import build_services
from imap_mcp.config import Config
from imap_mcp.entrypoints.mcp.app import build_server
from imapserver.server import PASSWORD, USERNAME

if TYPE_CHECKING:
    import ssl
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer

    from imap_mcp.core.services import Services
    from imapserver.server import FakeImapServer


@pytest.fixture
def config(tmp_path: Path, imap_server: FakeImapServer) -> Config:
    return Config(
        host="localhost",
        port=imap_server.port,
        username=USERNAME,
        from_address="ben@thecrisp.io",
        db_path=tmp_path / "state" / "imap-mcp.db",
        download_dir=tmp_path / "Downloads",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def services(
    config: Config, client_ssl_context: ssl.SSLContext, clock: FakeClock
) -> Services:
    return build_services(
        config,
        credentials=FakeCredentialSource(PASSWORD),
        ssl_context=client_ssl_context,
        clock=clock,
    )


@pytest.fixture
def server(services: Services) -> MCPServer:
    return build_server(services)
