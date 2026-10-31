"""Tests for the SQLite index, against a real database file."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from factories import make_parsed
from fakes import FakeClock
from imap_mcp.adapters.sqlite.connection import open_database
from imap_mcp.adapters.sqlite.index import SEARCH_ALL_SQL, SEARCH_TEXT_SQL, SqliteIndex
from imap_mcp.adapters.sqlite.schema import migrate
from imap_mcp.domain.enums import FolderRole, SyncStateKind
from imap_mcp.domain.model import FolderStatus, SearchQuery, SyncState
from imap_mcp.ports import Index

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from imap_mcp.domain.model import ParsedMessage

SEEN = frozenset({"\\Seen"})
UNSEEN: frozenset[str] = frozenset()
EXCLUDED = ("Junk", "Deleted Messages")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "imap-mcp.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def index(conn: sqlite3.Connection) -> SqliteIndex:
    idx = SqliteIndex(conn)
    for name, role in [
        ("INBOX", FolderRole.INBOX),
        ("Archive", FolderRole.ARCHIVE),
        ("Junk", FolderRole.JUNK),
        ("Deleted Messages", FolderRole.TRASH),
        ("Sent Messages", FolderRole.SENT),
    ]:
        idx.save_folder(name, role, None)
    return idx


def add(index: SqliteIndex, folder: str, uid: int, parsed: ParsedMessage,
        flags: frozenset[str] = UNSEEN) -> int:  # fmt: skip
    index.add_messages(folder, [(uid, flags, parsed)])
    return index.known_identities([parsed.identity])[parsed.identity]


def search(index: SqliteIndex, **options: Any) -> list[str]:
    page = index.search(SearchQuery(**options), EXCLUDED)
    return [hit.message.parsed.subject for hit in page.hits]


class TestDatabaseSetup:
    def test_the_index_satisfies_the_port(self, index: SqliteIndex) -> None:
        assert isinstance(index, Index)

    def test_files_are_private_to_the_user(
        self, conn: sqlite3.Connection, db_path: Path
    ) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = db_path.with_name(db_path.name + suffix)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, suffix
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700

    def test_settings_match_appendix_b(self, conn: sqlite3.Connection) -> None:
        def pragma(name: str) -> object:
            return conn.execute(f"PRAGMA {name}").fetchone()[0]

        assert pragma("journal_mode") == "wal"
        assert pragma("auto_vacuum") == 2  # incremental
        assert pragma("foreign_keys") == 1
        assert pragma("secure_delete") == 1
        assert pragma("user_version") == 1

    def test_a_double_quoted_unknown_column_is_an_error(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('SELECT "no_such_column" FROM messages')

    def test_migrating_twice_changes_nothing(self, conn: sqlite3.Connection) -> None:
        assert migrate(conn) == 1


class TestSearch:
    def test_a_subject_match_ranks_above_a_body_match(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed(subject="Lunch", body="the invoice is late"))
        add(index, "INBOX", 2, make_parsed(subject="Invoice 42", body="see attached"))

        assert search(index, text="invoice") == ["Invoice 42", "Lunch"]

    def test_the_snippet_marks_the_matched_term(self, index: SqliteIndex) -> None:
        add(
            index, "INBOX", 1, make_parsed(subject="Hi", body="Your policy renews soon")
        )

        (hit,) = index.search(SearchQuery(text="renews"), EXCLUDED).hits

        assert "[renews]" in hit.snippet
        assert len(hit.snippet) <= 200

    def test_junk_and_trash_are_skipped_unless_named(self, index: SqliteIndex) -> None:
        add(index, "Junk", 1, make_parsed(subject="Win a prize"))
        add(index, "Deleted Messages", 1, make_parsed(subject="Old prize"))

        assert search(index, text="prize") == []
        assert search(index, text="prize", folder="Junk") == ["Win a prize"]

    def test_fts_syntax_in_the_query_is_plain_text(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed(subject="Near the station"))

        assert search(index, text='NEAR( subject: "station" OR *') == []
        assert search(index, text="near station") == ["Near the station"]

    def test_filters_combine(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed(subject="A", sender="bob@acme.example",
            date=datetime(2026, 1, 5, tzinfo=UTC)), SEEN)  # fmt: skip
        add(index, "INBOX", 2, make_parsed(subject="B", sender="bob@acme.example",
            date=datetime(2026, 3, 5, tzinfo=UTC)))  # fmt: skip
        add(index, "INBOX", 3, make_parsed(subject="C", sender="eve@other.example",
            date=datetime(2026, 3, 6, tzinfo=UTC),
            attachments=[("a.pdf", "application/pdf", b"%PDF")]))  # fmt: skip

        assert search(index, from_address="ACME") == ["B", "A"]
        assert search(index, unread=True) == ["C", "B"]
        assert search(index, unread=False) == ["A"]
        assert search(index, since=datetime(2026, 3, 1, tzinfo=UTC)) == ["C", "B"]
        assert search(index, before=datetime(2026, 3, 6, tzinfo=UTC)) == ["B", "A"]
        assert search(index, has_attachment=True) == ["C"]

    def test_total_counts_every_page(self, index: SqliteIndex) -> None:
        for uid in range(1, 6):
            add(index, "INBOX", uid, make_parsed(subject=f"Report {uid}"))

        page = index.search(SearchQuery(text="report", limit=2, offset=2), EXCLUDED)

        assert page.total == 5
        assert len(page.hits) == 2

    def test_a_like_wildcard_in_a_filter_is_literal(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed(subject="A", sender="bob@acme.example"))

        assert search(index, from_address="%") == []


class TestCopiesAndDeletes:
    def test_two_copies_are_one_result_listing_both_folders(
        self, index: SqliteIndex
    ) -> None:
        parsed = make_parsed(subject="Copied")
        add(index, "INBOX", 7, parsed)
        add(index, "Archive", 9, parsed, SEEN)

        (hit,) = index.search(SearchQuery(text="copied"), EXCLUDED).hits

        assert hit.message.folders == ("Archive", "INBOX")
        assert hit.message.unread is False

    def test_a_message_without_locations_is_deleted_with_its_text(
        self, index: SqliteIndex
    ) -> None:
        add(index, "INBOX", 1, make_parsed(subject="Ephemeral"))

        index.remove_locations("INBOX", [1])
        assert index.delete_orphans() == 1

        assert search(index, text="ephemeral") == []
        assert index.stats().messages == 0

    def test_a_new_uidvalidity_drops_locations_and_marks_resync(
        self, index: SqliteIndex
    ) -> None:
        index.bind_uidvalidity("INBOX", FolderRole.INBOX, 5)
        status = FolderStatus(messages=1, uidnext=2, uidvalidity=5, unseen=0)
        add(index, "INBOX", 1, make_parsed())
        index.save_folder("INBOX", FolderRole.INBOX, status)

        assert index.bind_uidvalidity("INBOX", FolderRole.INBOX, 5) is False
        assert index.bind_uidvalidity("INBOX", FolderRole.INBOX, 6) is True

        assert index.folder_status("INBOX") is None
        assert index.folder_uids("INBOX") == set()

    def test_update_flags_counts_only_real_changes(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed())
        add(index, "INBOX", 2, make_parsed())

        changed = index.update_flags("INBOX", {1: SEEN, 2: UNSEEN})

        assert changed == 1


class TestKnownSenders:
    def test_an_address_i_wrote_to_is_known(self, index: SqliteIndex) -> None:
        add(index, "Sent Messages", 1, make_parsed(sender="ben@thecrisp.io",
            to="Plumber <fixit@plumb.example>"))  # fmt: skip

        known = index.known_addresses(["FIXIT@plumb.example", "x@y.example"])

        assert known == {"fixit@plumb.example"}

    def test_a_search_hit_reports_a_known_sender(self, index: SqliteIndex) -> None:
        add(index, "Sent Messages", 1, make_parsed(to="fixit@plumb.example"))
        add(
            index,
            "INBOX",
            1,
            make_parsed(subject="Quote", sender="fixit@plumb.example"),
        )

        (hit,) = index.search(SearchQuery(text="quote"), EXCLUDED).hits

        assert hit.message.known_sender is True


class TestSyncState:
    def test_state_round_trips(self, index: SqliteIndex) -> None:
        state = SyncState(
            last_success=datetime(2026, 10, 31, 21, 4, 12, tzinfo=UTC),
            last_attempt=datetime(2026, 10, 31, 21, 4, 0, tzinfo=UTC),
            kind=SyncStateKind.PARTIAL,
            remaining=12,
        )

        index.save_sync_state(state)

        assert index.sync_state() == state


class TestQueryPlans:
    @pytest.mark.parametrize(
        "query",
        [SearchQuery(text="invoice"), SearchQuery(), SearchQuery(folder="INBOX"),
         SearchQuery(unread=True, from_address="bob")],
        ids=["text", "list", "folder", "filters"],
    )  # fmt: skip
    def test_search_never_scans_messages_or_locations(
        self, conn: sqlite3.Connection, query: SearchQuery
    ) -> None:
        sql = SEARCH_TEXT_SQL if query.text else SEARCH_ALL_SQL
        params = {
            "match": '"invoice"', "folder": query.folder, "excluded": "[]",
            "since": None, "before": None, "from_like": None, "to_like": None,
            "has_attachment": None, "flagged": None, "unread": None,
            "limit": 10, "offset": 0,
        }  # fmt: skip
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]

        full_scans = [
            step
            for step in plan
            if step.startswith("SCAN")
            and step.split()[1] in {"m", "l", "messages", "locations"}
            and "USING" not in step
        ]
        assert full_scans == [], plan


class TestMaintenance:
    def test_many_deletes_give_pages_back_to_the_file_system(
        self, index: SqliteIndex
    ) -> None:
        filler = "lorem ipsum dolor sit amet " * 1000
        for uid in range(1, 1001):
            add(index, "INBOX", uid, make_parsed(subject=f"Bulk {uid}", body=filler))
        index.remove_locations("INBOX", range(1, 1001))
        index.delete_orphans()
        before = index.stats()

        freed = index.maintain(deadline=float("inf"), clock=FakeClock())

        after = index.stats()
        assert before.free_bytes > 16 * 1024 * 1024
        assert freed > 0
        assert after.file_bytes < before.file_bytes

    def test_a_few_deletes_leave_the_file_alone(self, index: SqliteIndex) -> None:
        add(index, "INBOX", 1, make_parsed())
        index.remove_locations("INBOX", [1])
        index.delete_orphans()

        assert index.maintain(deadline=float("inf"), clock=FakeClock()) == 0
