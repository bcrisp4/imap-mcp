"""Tests for the sync engine against the fake server and a real index."""

from __future__ import annotations

from typing import TYPE_CHECKING

from factories import make_message
from fakes import FakeCredentialSource
from imap_mcp.adapters.imap.session import ImapMailSourceFactory
from imap_mcp.domain.enums import SyncStateKind
from imap_mcp.domain.model import SearchQuery
from imapserver.server import PASSWORD, USERNAME, Quirks

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable

    from fakes import FakeClock, FakeLock
    from imap_mcp.adapters.sqlite.index import SqliteIndex
    from imap_mcp.core.sync import SyncService
    from imapserver.server import FakeImapServer

ALL = ()


def subjects(index: SqliteIndex, folder: str | None = None) -> list[str]:
    page = index.search(SearchQuery(folder=folder, limit=100), ALL)
    return sorted(h.message.parsed.subject for h in page.hits)


def commands_since(server: FakeImapServer, start: int) -> list[str]:
    return [c.line.split(" ", 1)[1] for c in server.commands[start:]]


class TestFullSync:
    def test_every_folder_is_indexed(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        imap_server.add_message("INBOX", make_message(subject="Hello"))
        imap_server.add_message("Archive", make_message(subject="Old"))
        imap_server.add_message("Junk", make_message(subject="Spam"))

        report = make_sync().full_sync()

        assert report.kind is SyncStateKind.COMPLETE
        assert report.new_messages == 3
        assert subjects(index) == ["Hello", "Old", "Spam"]

    def test_attachment_bytes_never_cross_the_network(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService]
    ) -> None:
        imap_server.add_message(
            "INBOX",
            make_message(attachments=[("big.pdf", "application/pdf", b"%PDF" * 1000)]),
        )

        make_sync().full_sync()

        fetches = [c for c in commands_since(imap_server, 0) if "FETCH" in c]
        assert any("BODY.PEEK[1]<0." in c for c in fetches)
        assert not any("BODY.PEEK[2]" in c or "BODY.PEEK[]" in c for c in fetches)

    def test_a_second_sync_with_no_change_sends_only_status(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService]
    ) -> None:
        imap_server.add_message("INBOX", make_message())
        sync = make_sync()
        sync.full_sync()
        start = len(imap_server.commands)

        sync.full_sync()

        verbs = {c.split(" ")[0] for c in commands_since(imap_server, start)}
        assert verbs <= {"CAPABILITY", "LOGIN", "LIST", "STATUS", "LOGOUT"}


class TestIncrementalSync:
    def test_a_new_message_is_fetched_from_its_folder_only(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        imap_server.add_message("Archive", make_message(subject="Old"))
        sync = make_sync()
        sync.full_sync()
        imap_server.add_message("INBOX", make_message(subject="Invoice"))
        start = len(imap_server.commands)

        report = sync.full_sync()

        assert report.new_messages == 1
        assert report.folders == ("INBOX",)
        examined = [
            c for c in commands_since(imap_server, start) if c.startswith("EXAMINE")
        ]
        assert examined == ['EXAMINE "INBOX"']
        assert subjects(index) == ["Invoice", "Old"]

    def test_a_deleted_message_leaves_the_index(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        uid = imap_server.add_message("INBOX", make_message(subject="Gone"))
        sync = make_sync()
        sync.full_sync()

        imap_server.remove_message("INBOX", uid)
        report = sync.full_sync()

        assert report.removed == 1
        assert subjects(index) == []
        assert index.stats().messages == 0

    def test_a_moved_message_is_linked_and_not_downloaded_again(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        raw = make_message(subject="Moving")
        uid = imap_server.add_message("INBOX", raw)
        sync = make_sync()
        sync.full_sync()
        start = len(imap_server.commands)

        imap_server.remove_message("INBOX", uid)
        imap_server.add_message("Archive", raw)
        report = sync.full_sync()

        assert (report.new_messages, report.linked) == (0, 1)
        assert not any("BODY.PEEK[1]" in c for c in commands_since(imap_server, start))
        assert subjects(index, folder="Archive") == ["Moving"]

    def test_a_flag_change_arrives_through_changedsince(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        uid = imap_server.add_message("INBOX", make_message(subject="Read me"))
        sync = make_sync()
        sync.full_sync()
        start = len(imap_server.commands)

        imap_server.set_flags("INBOX", uid, ["\\Seen", "\\Flagged"])
        report = sync.full_sync()

        assert report.flags_updated == 1
        assert any("CHANGEDSINCE" in c for c in commands_since(imap_server, start))
        (hit,) = index.search(SearchQuery(), ALL).hits
        assert (hit.message.unread, hit.message.flagged) == (False, True)


class TestUidValidity:
    def test_a_renumbered_folder_is_relinked_without_downloads(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        for n in range(3):
            imap_server.add_message("INBOX", make_message(subject=f"M{n}"))
        sync = make_sync()
        sync.full_sync()
        start = len(imap_server.commands)

        imap_server.renumber("INBOX", uidvalidity=99)
        report = sync.full_sync()

        assert (report.new_messages, report.linked) == (0, 3)
        assert index.folder_uids("INBOX") == {4, 5, 6}
        assert not any("BODY.PEEK[1]" in c for c in commands_since(imap_server, start))

    def test_an_interrupted_relink_resumes_on_the_next_run(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex, clock: FakeClock,
    ) -> None:  # fmt: skip
        for n in range(4):
            imap_server.add_message("INBOX", make_message(subject=f"M{n}"))
        make_sync(batch_size=1).full_sync()
        imap_server.renumber("INBOX", uidvalidity=99)

        clock.step = 6.0  # each clock read moves 6 s, so the 20 s budget ends early
        partial = make_sync(batch_size=1).sync_now()
        clock.step = 0.0
        complete = make_sync(batch_size=1).sync_now()

        assert partial.kind is SyncStateKind.PARTIAL
        assert complete.kind is SyncStateKind.COMPLETE
        assert index.folder_uids("INBOX") == {5, 6, 7, 8}
        assert index.stats().messages == 4


class TestBudgetAndFreshness:
    def test_an_expired_budget_reports_a_partial_sync_with_the_remainder(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex, clock: FakeClock,
    ) -> None:  # fmt: skip
        for n in range(10):
            imap_server.add_message("INBOX", make_message(subject=f"M{n}"))
        clock.step = 4.0

        report = make_sync(batch_size=2).sync_now()

        assert report.kind is SyncStateKind.PARTIAL
        assert report.remaining > 0
        assert index.sync_state().kind is SyncStateKind.PARTIAL
        assert index.sync_state().last_success is None

    def test_newest_messages_are_fetched_first(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        index: SqliteIndex, clock: FakeClock,
    ) -> None:  # fmt: skip
        for n in range(4):
            imap_server.add_message("INBOX", make_message(subject=f"M{n}"))
        clock.step = 7.0

        make_sync(batch_size=1).sync_now()

        assert "M3" in subjects(index)
        assert "M0" not in subjects(index)

    def test_a_tool_call_within_the_interval_does_not_connect(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        clock: FakeClock,
    ) -> None:  # fmt: skip
        sync = make_sync()
        sync.ensure_fresh()
        start = len(imap_server.commands)

        clock.advance(14 * 60)
        state = sync.ensure_fresh()

        assert len(imap_server.commands) == start
        assert state.kind is SyncStateKind.COMPLETE

    def test_a_tool_call_after_the_interval_syncs(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        clock: FakeClock, index: SqliteIndex,
    ) -> None:  # fmt: skip
        sync = make_sync()
        sync.ensure_fresh()
        imap_server.add_message("INBOX", make_message(subject="Invoice"))

        clock.advance(16 * 60)
        sync.ensure_fresh()

        assert subjects(index) == ["Invoice"]

    def test_another_process_holding_the_lock_means_no_sync_here(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        lock: FakeLock,
    ) -> None:  # fmt: skip
        lock.held_elsewhere = True

        state = make_sync().ensure_fresh()
        report = make_sync().sync_now()

        assert state.kind is SyncStateKind.IN_PROGRESS_ELSEWHERE
        assert report.kind is SyncStateKind.IN_PROGRESS_ELSEWHERE
        assert imap_server.commands == []

    def test_a_failed_sync_is_recorded_and_not_retried_within_the_interval(
        self, imap_server: FakeImapServer, make_sync: Callable[..., SyncService],
        source_factory: Callable[..., ImapMailSourceFactory], clock: FakeClock,
    ) -> None:  # fmt: skip
        sync = make_sync(sources=source_factory(password="wrong"))

        first = sync.ensure_fresh()
        logins = imap_server.command_verbs().count("LOGIN")
        clock.advance(60)
        sync.ensure_fresh()

        assert first.kind is SyncStateKind.FAILED
        assert first.error is not None
        assert "LOGIN" in first.error
        assert imap_server.command_verbs().count("LOGIN") == logins


class TestFlagFallbacks:
    def test_without_condstore_an_unread_change_refreshes_flags(
        self, imap_server_factory: Callable[..., FakeImapServer],
        client_ssl_context: ssl.SSLContext, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        server = imap_server_factory(Quirks(condstore=False))
        server.add_mailbox("INBOX")
        uid = server.add_message("INBOX", make_message(subject="Read me"))
        sources = ImapMailSourceFactory(
            host="localhost", port=server.port, username=USERNAME,
            credentials=FakeCredentialSource(PASSWORD), ssl_context=client_ssl_context,
        )  # fmt: skip
        sync = make_sync(sources=sources)
        sync.full_sync()

        server.set_flags("INBOX", uid, ["\\Seen"])
        report = sync.full_sync()

        assert report.flags_updated == 1
        (hit,) = index.search(SearchQuery(), ALL).hits
        assert hit.message.unread is False

    def test_sync_now_for_a_folder_catches_a_flag_that_status_cannot_show(
        self, imap_server_factory: Callable[..., FakeImapServer],
        client_ssl_context: ssl.SSLContext, make_sync: Callable[..., SyncService],
        index: SqliteIndex,
    ) -> None:  # fmt: skip
        server = imap_server_factory(Quirks(condstore=False))
        server.add_mailbox("INBOX")
        uid = server.add_message("INBOX", make_message(), flags=["\\Seen"])
        sources = ImapMailSourceFactory(
            host="localhost", port=server.port, username=USERNAME,
            credentials=FakeCredentialSource(PASSWORD), ssl_context=client_ssl_context,
        )  # fmt: skip
        sync = make_sync(sources=sources)
        sync.full_sync()

        server.set_flags("INBOX", uid, ["\\Seen", "\\Flagged"])
        assert sync.full_sync().flags_updated == 0
        report = sync.sync_now("INBOX")

        assert report.flags_updated == 1
        (hit,) = index.search(SearchQuery(), ALL).hits
        assert hit.message.flagged is True
