"""Tests for the IMAP adapter against the TLS fake server."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any

import pytest
from imapclient import IMAPClient

from factories import make_message
from fakes import FakeCredentialSource
from imap_mcp.adapters.imap.guard import install_guard
from imap_mcp.adapters.imap.session import ImapMailSourceFactory
from imap_mcp.domain.enums import FolderRole
from imap_mcp.domain.errors import ConfigError, GuardViolation, MailError
from imap_mcp.ports import MailSource, MailSourceFactory
from imapserver.server import PASSWORD, USERNAME, FakeImapServer, Quirks

if TYPE_CHECKING:
    from collections.abc import Callable


def factory_for(
    server: FakeImapServer, context: ssl.SSLContext, **overrides: Any
) -> ImapMailSourceFactory:
    options: dict[str, Any] = {
        "host": "localhost",
        "port": server.port,
        "username": USERNAME,
        "credentials": FakeCredentialSource(PASSWORD),
        "ssl_context": context,
    }
    options.update(overrides)
    return ImapMailSourceFactory(**options)


class TestConnection:
    def test_the_factory_and_session_satisfy_the_ports(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        factory = factory_for(imap_server, client_ssl_context)

        with factory.open() as source:
            assert isinstance(source, MailSource)
        assert isinstance(factory, MailSourceFactory)

    def test_a_certificate_from_an_unknown_ca_is_refused(
        self, imap_server: FakeImapServer
    ) -> None:
        factory = factory_for(imap_server, ssl.create_default_context())

        with pytest.raises(MailError, match="connect"), factory.open():
            pass

    def test_a_wrong_password_raises_mail_error(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        factory = factory_for(
            imap_server, client_ssl_context, credentials=FakeCredentialSource("wrong")
        )

        with pytest.raises(MailError, match="LOGIN"), factory.open():
            pass

    def test_the_session_ends_with_logout_and_never_close(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        with factory_for(imap_server, client_ssl_context).open() as source:
            source.examine("INBOX")

        verbs = imap_server.command_verbs()
        assert verbs[-1] == "LOGOUT"
        assert "CLOSE" not in verbs
        assert "SELECT" not in verbs


class TestFolders:
    def test_roles_come_from_names_when_the_server_has_no_special_use(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        with factory_for(imap_server, client_ssl_context).open() as source:
            roles = {f.name: f.role for f in source.list_folders()}

        assert roles == {
            "INBOX": FolderRole.INBOX,
            "Archive": FolderRole.ARCHIVE,
            "Drafts": FolderRole.DRAFTS,
            "Sent Messages": FolderRole.SENT,
            "Junk": FolderRole.JUNK,
        }

    def test_status_reports_counts_and_highestmodseq(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        imap_server.add_message("INBOX", make_message())
        imap_server.add_message("INBOX", make_message(), flags=["\\Seen"])

        with factory_for(imap_server, client_ssl_context).open() as source:
            status = source.status("INBOX")

        assert (status.messages, status.uidnext, status.unseen) == (2, 3, 1)
        assert status.highestmodseq is not None

    def test_status_omits_highestmodseq_without_condstore(
        self,
        imap_server_factory: Callable[..., FakeImapServer],
        client_ssl_context: ssl.SSLContext,
    ) -> None:
        server = imap_server_factory(Quirks(condstore=False))
        server.add_mailbox("INBOX")

        with factory_for(server, client_ssl_context).open() as source:
            assert source.status("INBOX").highestmodseq is None


class TestFetch:
    def test_headers_and_structure_come_back_for_each_uid(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        raw = make_message(
            html="<p>Hello</p>",
            attachments=[("renewal.pdf", "application/pdf", b"%PDF-1.7 data")],
        )
        uid = imap_server.add_message("INBOX", raw, flags=["\\Seen"])

        with factory_for(imap_server, client_ssl_context).open() as source:
            source.examine("INBOX")
            (header,) = source.fetch_headers([uid])

        assert header.uid == uid
        assert header.flags == frozenset({"\\Seen"})
        assert b"Subject: Your car insurance renewal" in header.header
        assert header.structure is not None
        assert [t.subtype for t in header.structure.text_parts] == ["html"]
        (attachment,) = header.structure.attachments
        assert (attachment.filename, attachment.content_type) == (
            "renewal.pdf",
            "application/pdf",
        )
        assert header.internal_date.tzinfo is not None

    def test_text_fetch_is_capped_at_max_bytes(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        uid = imap_server.add_message("INBOX", make_message(body="x" * 5000))

        with factory_for(imap_server, client_ssl_context).open() as source:
            source.examine("INBOX")
            (header,) = source.fetch_headers([uid])
            assert header.structure is not None
            texts = source.fetch_text([uid], header.structure.text_parts, 100)

        assert len(texts[uid]["1"]) == 100

    def test_an_unsolicited_flag_update_does_not_land_on_another_uid(
        self,
        imap_server_factory: Callable[..., FakeImapServer],
        client_ssl_context: ssl.SSLContext,
    ) -> None:
        server = imap_server_factory(Quirks(unsolicited_flags_seq=1))
        server.add_mailbox("INBOX")
        server.add_message("INBOX", make_message())
        server.remove_message("INBOX", 1)
        uid = server.add_message("INBOX", make_message())  # UID 2, sequence 1
        server.add_message("INBOX", make_message())  # UID 3

        with factory_for(server, client_ssl_context).open() as source:
            source.examine("INBOX")
            flags = source.fetch_flags([uid, 1])

        assert flags == {uid: frozenset()}

    def test_search_by_message_id_finds_the_uid(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        imap_server.add_message("INBOX", make_message())
        uid = imap_server.add_message("INBOX", make_message(message_id="<x@test>"))

        with factory_for(imap_server, client_ssl_context).open() as source:
            source.examine("INBOX")
            assert source.search_message_id("<x@test>") == [uid]
            assert source.search_uids() == [1, uid]


class TestDrafts:
    def test_append_writes_to_drafts_with_the_draft_flag(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        with factory_for(imap_server, client_ssl_context).open() as source:
            assert source.drafts_folder() == "Drafts"
            source.append_draft("Drafts", make_message(subject="Re: hi"))

        (stored,) = imap_server.mailboxes["Drafts"].messages
        assert "\\Draft" in stored.flags

    def test_append_to_any_other_folder_is_refused_before_it_is_sent(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        with (
            factory_for(imap_server, client_ssl_context).open() as source,
            pytest.raises(GuardViolation),
        ):
            source.drafts_folder()
            source.append_draft("INBOX", make_message())

        assert imap_server.mailboxes["INBOX"].messages == []
        assert "APPEND" not in imap_server.command_verbs()

    def test_a_missing_configured_drafts_folder_names_the_setting(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        factory = factory_for(imap_server, client_ssl_context, drafts_folder="Draft")

        with factory.open() as source, pytest.raises(ConfigError, match="'Draft'"):
            source.drafts_folder()

    def test_opening_a_session_sends_no_list(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        with factory_for(imap_server, client_ssl_context).open():
            pass

        assert "LIST" not in imap_server.command_verbs()

    def test_a_raw_write_on_a_guarded_client_is_refused(
        self, imap_server: FakeImapServer, client_ssl_context: ssl.SSLContext
    ) -> None:
        # imapclient is untyped, so the test drives it through Any.
        client: Any = IMAPClient(
            "localhost", port=imap_server.port, ssl_context=client_ssl_context
        )
        install_guard(client)
        client.login(USERNAME, PASSWORD)
        client.select_folder("INBOX", readonly=True)

        with pytest.raises(GuardViolation):
            client.select_folder("INBOX")
        with pytest.raises(GuardViolation):
            client.add_flags([1], [b"\\Seen"])
        client.logout()

        assert "SELECT" not in imap_server.command_verbs()
        assert "UID STORE" not in imap_server.command_verbs()
