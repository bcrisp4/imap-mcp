"""The SQLite index adapter. Implements `Index`.

Every statement is static SQL with bound parameters. A list parameter goes in
as one JSON array and is read back with `json_each`, so no SQL text is built
at run time. Each write method runs one short `BEGIN IMMEDIATE` transaction,
and no method makes a network call.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from imap_mcp.adapters.sqlite.connection import write_transaction
from imap_mcp.adapters.sqlite.fts import build_match
from imap_mcp.domain.enums import AuthVerdict, FolderRole, ParseState, SyncStateKind
from imap_mcp.domain.model import (
    AttachmentInfo,
    AuthResults,
    FolderStatus,
    FolderSummary,
    IndexStats,
    ParsedMessage,
    SearchHit,
    SearchPage,
    StoredMessage,
    SyncState,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from imap_mcp.domain.model import SearchQuery
    from imap_mcp.ports import Clock

SNIPPET_CHARS: Final = 200
_SEEN: Final = "\\Seen"
_FLAGGED: Final = "\\Flagged"
_VACUUM_STEP_PAGES: Final = 1000
_VACUUM_MIN_FREE_BYTES: Final = 16 * 1024 * 1024
_VACUUM_MIN_FREE_RATIO: Final = 0.10

# Folder scope: a named folder, or any folder outside the excluded list.
# The statements below join fixed fragments at import time. Every value is a
# bound parameter, and no input reaches the SQL text.
_SCOPE: Final = """
    (
        (:folder IS NOT NULL AND EXISTS (
            SELECT 1 FROM locations l JOIN folders f ON f.id = l.folder_id
            WHERE l.message_id = m.id AND f.name = :folder))
        OR (:folder IS NULL AND EXISTS (
            SELECT 1 FROM locations l JOIN folders f ON f.id = l.folder_id
            WHERE l.message_id = m.id
              AND f.name NOT IN (SELECT value FROM json_each(:excluded))))
    )
    AND (:since IS NULL OR m.date >= :since)
    AND (:before IS NULL OR m.date < :before)
    AND (:from_like IS NULL OR m.from_address LIKE :from_like ESCAPE '\\'
         OR m.from_name LIKE :from_like ESCAPE '\\')
    AND (:to_like IS NULL OR m.to_addrs LIKE :to_like ESCAPE '\\'
         OR m.cc_addrs LIKE :to_like ESCAPE '\\')
    AND (:has_attachment IS NULL OR m.has_attachments = :has_attachment)
    AND (:flagged IS NULL OR :flagged = EXISTS (
        SELECT 1 FROM locations l WHERE l.message_id = m.id AND l.flagged = 1))
    AND (:unread IS NULL OR :unread = NOT EXISTS (
        SELECT 1 FROM locations l WHERE l.message_id = m.id AND l.seen = 1))
"""

_TEXT_FROM: Final = """
    FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
    WHERE messages_fts MATCH :match AND
"""
_TEXT_SELECT: Final = (
    "SELECT m.id, snippet(messages_fts, 3, '[', ']', '...', 24) AS snippet"
)
_TEXT_ORDER: Final = """
    ORDER BY bm25(messages_fts, 10.0, 5.0, 3.0, 1.0), m.date DESC
    LIMIT :limit OFFSET :offset
"""
_ALL_SELECT: Final = """
    SELECT m.id, substr(m.body, 1, 200) AS snippet
    FROM messages m INDEXED BY messages_date WHERE
"""
_ALL_ORDER: Final = "ORDER BY m.date DESC LIMIT :limit OFFSET :offset"
_COUNT: Final = "SELECT count(*)"

SEARCH_TEXT_SQL: Final = _TEXT_SELECT + _TEXT_FROM + _SCOPE + _TEXT_ORDER
_COUNT_TEXT: Final = _COUNT + _TEXT_FROM + _SCOPE
SEARCH_ALL_SQL: Final = _ALL_SELECT + _SCOPE + _ALL_ORDER
_COUNT_ALL: Final = _COUNT + " FROM messages m WHERE " + _SCOPE

_MESSAGE_ROWS: Final = """
    SELECT m.*,
        (SELECT json_group_array(f.name) FROM locations l
         JOIN folders f ON f.id = l.folder_id WHERE l.message_id = m.id) AS folders,
        NOT EXISTS (SELECT 1 FROM locations l
                    WHERE l.message_id = m.id AND l.seen = 1) AS unread,
        EXISTS (SELECT 1 FROM locations l
                WHERE l.message_id = m.id AND l.flagged = 1) AS flagged_any
    FROM messages m WHERE m.id IN (SELECT value FROM json_each(:ids))
"""

_KNOWN_ADDRESSES: Final = """
    SELECT DISTINCT a.value FROM json_each(:addresses) a
    WHERE EXISTS (
        SELECT 1 FROM folders f
        JOIN locations l ON l.folder_id = f.id
        JOIN messages m ON m.id = l.message_id
        WHERE f.role = :sent
          AND (instr(char(10) || m.to_addrs || char(10),
                     char(10) || a.value || char(10)) > 0
               OR instr(char(10) || m.cc_addrs || char(10),
                        char(10) || a.value || char(10)) > 0))
"""


def _ts(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _dt(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


def _like(value: str | None) -> str | None:
    if not value:
        return None
    escaped = (
        value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _json(values: Collection[Any]) -> str:
    return json.dumps(list(values))


def _tri(value: object) -> int | None:
    return None if value is None else int(bool(value))


def _verdict(value: str) -> AuthVerdict:
    try:
        return AuthVerdict(value)
    except ValueError:
        return AuthVerdict.UNKNOWN


class SqliteIndex:
    """The message index in one SQLite file."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap an open, migrated connection.

        Args:
            conn: A connection from `open_database`, after `migrate`.
        """
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # -- folders ------------------------------------------------------------------

    def folder_status(self, folder: str) -> FolderStatus | None:
        """Return the status stored after the last complete folder sync."""
        row = self._conn.execute(
            "SELECT * FROM folders WHERE name = ? AND synced = 1", (folder,)
        ).fetchone()
        if row is None:
            return None
        return FolderStatus(
            messages=row["messages"],
            uidnext=row["uidnext"],
            uidvalidity=row["uidvalidity"],
            unseen=row["unseen"],
            highestmodseq=row["highestmodseq"],
        )

    def save_folder(
        self, folder: str, role: FolderRole, status: FolderStatus | None
    ) -> None:
        """Record a folder and its status. None marks the folder for resync."""
        values = {
            "name": folder,
            "role": role.value,
            "synced": int(status is not None),
            "uidvalidity": status.uidvalidity if status else None,
            "uidnext": status.uidnext if status else None,
            "messages": status.messages if status else None,
            "unseen": status.unseen if status else None,
            "highestmodseq": status.highestmodseq if status else None,
        }
        with write_transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO folders (name, role, synced, uidvalidity, uidnext,
                                     messages, unseen, highestmodseq)
                VALUES (:name, :role, :synced, :uidvalidity, :uidnext,
                        :messages, :unseen, :highestmodseq)
                ON CONFLICT (name) DO UPDATE SET
                    role = excluded.role, synced = excluded.synced,
                    uidvalidity = excluded.uidvalidity, uidnext = excluded.uidnext,
                    messages = excluded.messages, unseen = excluded.unseen,
                    highestmodseq = excluded.highestmodseq
                """,
                values,
            )

    def remove_folders_except(self, names: Collection[str]) -> int:
        """Delete folders, and their locations, not in `names`."""
        with write_transaction(self._conn):
            cursor = self._conn.execute(
                "DELETE FROM folders"
                " WHERE name NOT IN (SELECT value FROM json_each(?))",
                (_json(names),),
            )
            return cursor.rowcount

    def folder_uidvalidity(self, folder: str) -> int | None:
        """Return the UIDVALIDITY that the folder's stored UIDs belong to."""
        row = self._conn.execute(
            "SELECT uidvalidity FROM folders WHERE name = ?", (folder,)
        ).fetchone()
        return None if row is None else row["uidvalidity"]

    def folder_uids(self, folder: str) -> set[int]:
        """Return the UIDs the index holds for a folder."""
        rows = self._conn.execute(
            "SELECT l.uid FROM locations l JOIN folders f ON f.id = l.folder_id"
            " WHERE f.name = ?",
            (folder,),
        )
        return {row["uid"] for row in rows}

    def remove_locations(self, folder: str, uids: Collection[int]) -> int:
        """Delete locations that the server no longer has."""
        if not uids:
            return 0
        with write_transaction(self._conn):
            cursor = self._conn.execute(
                """
                DELETE FROM locations
                WHERE folder_id = (SELECT id FROM folders WHERE name = ?)
                  AND uid IN (SELECT value FROM json_each(?))
                """,
                (folder, _json(uids)),
            )
            return cursor.rowcount

    def bind_uidvalidity(self, folder: str, role: FolderRole, uidvalidity: int) -> bool:
        """Record the UIDVALIDITY that the folder's locations belong to.

        A changed value deletes the folder's locations at once and leaves the
        folder unsynced. The folder status is stored only after the folder
        sync completes, so an interrupted relink resumes on the next run.
        """
        with write_transaction(self._conn):
            row = self._conn.execute(
                "SELECT id, uidvalidity FROM folders WHERE name = ?", (folder,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO folders (name, role, uidvalidity) VALUES (?, ?, ?)",
                    (folder, role.value, uidvalidity),
                )
                return False
            changed = (
                row["uidvalidity"] is not None and row["uidvalidity"] != uidvalidity
            )
            if changed:
                self._conn.execute(
                    "DELETE FROM locations WHERE folder_id = ?", (row["id"],)
                )
            self._conn.execute(
                "UPDATE folders SET role = ?, uidvalidity = ?,"
                " synced = CASE WHEN ? THEN 0 ELSE synced END WHERE id = ?",
                (role.value, uidvalidity, int(changed), row["id"]),
            )
            return changed

    def folders_with_role(self, roles: Collection[FolderRole]) -> list[str]:
        """Return the names of folders with any of these roles."""
        rows = self._conn.execute(
            "SELECT name FROM folders WHERE role IN (SELECT value FROM json_each(?))"
            " ORDER BY name",
            (_json([r.value for r in roles]),),
        )
        return [row["name"] for row in rows]

    def list_folders(self) -> list[FolderSummary]:
        """Return every folder with its counts."""
        rows = self._conn.execute(
            """
            SELECT f.name, f.role, count(l.uid) AS messages,
                   coalesce(sum(1 - l.seen), 0) AS unread
            FROM folders f LEFT JOIN locations l ON l.folder_id = f.id
            GROUP BY f.id ORDER BY f.name
            """
        )
        return [
            FolderSummary(
                name=row["name"],
                role=FolderRole(row["role"]),
                messages=row["messages"],
                unread=row["unread"],
            )
            for row in rows
        ]

    # -- messages -------------------------------------------------------------------

    def known_identities(self, identities: Collection[str]) -> dict[str, int]:
        """Return the message IDs of identities already in the index."""
        if not identities:
            return {}
        rows = self._conn.execute(
            "SELECT identity, id FROM messages"
            " WHERE identity IN (SELECT value FROM json_each(?))",
            (_json(identities),),
        )
        return {row["identity"]: row["id"] for row in rows}

    def add_messages(
        self,
        folder: str,
        items: Sequence[tuple[int, frozenset[str], ParsedMessage]],
    ) -> int:
        """Store new messages with their locations. Return the number added."""
        added = 0
        with write_transaction(self._conn):
            folder_id = self._folder_id(folder)
            for uid, flags, parsed in items:
                cursor = self._conn.execute(_INSERT_MESSAGE, _message_values(parsed))
                added += cursor.rowcount
                message_id = self._conn.execute(
                    "SELECT id FROM messages WHERE identity = ?", (parsed.identity,)
                ).fetchone()["id"]
                for attachment in parsed.attachments:
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO attachments
                            (message_id, part, filename, content_type, size, encoding)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            attachment.part,
                            attachment.filename,
                            attachment.content_type,
                            attachment.size,
                            attachment.encoding,
                        ),
                    )
                self._put_location(folder_id, uid, flags, message_id)
        return added

    def link_locations(
        self, folder: str, items: Sequence[tuple[int, frozenset[str], int]]
    ) -> int:
        """Add locations, as (uid, flags, message id), for stored messages."""
        with write_transaction(self._conn):
            folder_id = self._folder_id(folder)
            for uid, flags, message_id in items:
                self._put_location(folder_id, uid, flags, message_id)
        return len(items)

    def update_flags(self, folder: str, flags: Mapping[int, frozenset[str]]) -> int:
        """Replace the flags of locations. Return the number changed."""
        changed = 0
        with write_transaction(self._conn):
            folder_id = self._folder_id(folder)
            for uid, values in flags.items():
                cursor = self._conn.execute(
                    """
                    UPDATE locations SET seen = ?, flagged = ?, flags = ?
                    WHERE folder_id = ? AND uid = ? AND flags != ?
                    """,
                    (
                        int(_SEEN in values),
                        int(_FLAGGED in values),
                        _flags_text(values),
                        folder_id,
                        uid,
                        _flags_text(values),
                    ),
                )
                changed += cursor.rowcount
        return changed

    def delete_orphans(self) -> int:
        """Delete messages that no folder holds."""
        with write_transaction(self._conn):
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE NOT EXISTS"
                " (SELECT 1 FROM locations l WHERE l.message_id = messages.id)"
            )
            return cursor.rowcount

    def get_message(self, message_id: int) -> StoredMessage | None:
        """Return one message, or None."""
        found = self._hydrate([message_id])
        return found[0] if found else None

    def locations(self, message_id: int) -> list[tuple[str, int]]:
        """Return the (folder, uid) pairs of a message."""
        rows = self._conn.execute(
            "SELECT f.name, l.uid FROM locations l JOIN folders f ON f.id = l.folder_id"
            " WHERE l.message_id = ? ORDER BY f.name",
            (message_id,),
        )
        return [(row["name"], row["uid"]) for row in rows]

    def known_addresses(self, addresses: Collection[str]) -> set[str]:
        """Return the addresses, lower case, that I sent mail to."""
        wanted = {a.lower() for a in addresses if a}
        if not wanted:
            return set()
        rows = self._conn.execute(
            _KNOWN_ADDRESSES,
            {"addresses": _json(wanted), "sent": FolderRole.SENT.value},
        )
        return {row["value"] for row in rows}

    # -- search ---------------------------------------------------------------------

    def search(
        self, query: SearchQuery, excluded_folders: Collection[str]
    ) -> SearchPage:
        """Run a search. Skip `excluded_folders` unless the query names one."""
        params: dict[str, Any] = {
            "folder": query.folder,
            "excluded": _json(excluded_folders),
            "since": _ts(query.since),
            "before": _ts(query.before),
            "from_like": _like(query.from_address),
            "to_like": _like(query.to_address),
            "has_attachment": _tri(query.has_attachment),
            "flagged": _tri(query.flagged),
            "unread": _tri(query.unread),
            "limit": query.limit,
            "offset": query.offset,
        }
        match = build_match(query.text) if query.text else None
        if query.text and match is None:
            return SearchPage(total=0, hits=())
        if match is not None:
            params["match"] = match
            rows = self._conn.execute(SEARCH_TEXT_SQL, params).fetchall()
            total = self._conn.execute(_COUNT_TEXT, params).fetchone()[0]
        else:
            rows = self._conn.execute(SEARCH_ALL_SQL, params).fetchall()
            total = self._conn.execute(_COUNT_ALL, params).fetchone()[0]
        messages = {m.id: m for m in self._hydrate([row["id"] for row in rows])}
        hits = tuple(
            SearchHit(message=messages[row["id"]], snippet=_clip(row["snippet"]))
            for row in rows
            if row["id"] in messages
        )
        return SearchPage(total=int(total), hits=hits)

    # -- sync state -----------------------------------------------------------------

    def sync_state(self) -> SyncState:
        """Return the shared sync state."""
        row = self._conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        if row is None:
            return SyncState()
        return SyncState(
            last_success=_dt(row["last_success"]),
            last_attempt=_dt(row["last_attempt"]),
            kind=SyncStateKind(row["kind"]),
            error=row["error"],
            remaining=row["remaining"],
        )

    def save_sync_state(self, state: SyncState) -> None:
        """Replace the shared sync state."""
        with write_transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO sync_state (id, last_success, last_attempt, kind, error,
                                        remaining)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    last_success = excluded.last_success,
                    last_attempt = excluded.last_attempt, kind = excluded.kind,
                    error = excluded.error, remaining = excluded.remaining
                """,
                (
                    _ts(state.last_success), _ts(state.last_attempt), state.kind.value,
                    state.error, state.remaining,
                ),
            )  # fmt: skip

    # -- maintenance ----------------------------------------------------------------

    def maintain(self, deadline: float, clock: Clock) -> int:
        """Free unused pages until `deadline`, then refresh planner statistics.

        It runs only when free pages are more than 10% of the file and more than
        16 MB, so a normal sync with few deletes does nothing here.
        """
        freed = 0
        page_size = self._pragma_int("page_size")
        free = self._pragma_int("freelist_count")
        total = max(self._pragma_int("page_count"), 1)
        if (
            free / total > _VACUUM_MIN_FREE_RATIO
            and free * page_size > _VACUUM_MIN_FREE_BYTES
        ):
            while free > 0 and clock.monotonic() < deadline:
                self._conn.execute(f"PRAGMA incremental_vacuum({_VACUUM_STEP_PAGES:d})")
                remaining = self._pragma_int("freelist_count")
                freed += free - remaining
                free = remaining
        self._conn.execute("PRAGMA optimize")
        return freed

    def stats(self) -> IndexStats:
        """Return size figures."""
        page_size = self._pragma_int("page_size")
        messages = self._conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        return IndexStats(
            messages=int(messages),
            file_bytes=self._pragma_int("page_count") * page_size,
            free_bytes=self._pragma_int("freelist_count") * page_size,
        )

    def vacuum(self) -> None:
        """Rebuild the file and merge the full-text index segments."""
        with write_transaction(self._conn):
            self._conn.execute(
                "INSERT INTO messages_fts (messages_fts) VALUES ('optimize')"
            )
        self._conn.execute("VACUUM")

    # -- helpers --------------------------------------------------------------------

    def _pragma_int(self, name: str) -> int:
        return int(self._conn.execute(f"PRAGMA {name}").fetchone()[0])

    def _folder_id(self, folder: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM folders WHERE name = ?", (folder,)
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
                "INSERT INTO folders (name, role) VALUES (?, ?)",
                (folder, FolderRole.OTHER.value),
            )
            return int(cursor.lastrowid or 0)
        return int(row["id"])

    def _put_location(
        self, folder_id: int, uid: int, flags: frozenset[str], message_id: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO locations (folder_id, uid, message_id, seen, flagged, flags)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (folder_id, uid) DO UPDATE SET
                message_id = excluded.message_id, seen = excluded.seen,
                flagged = excluded.flagged, flags = excluded.flags
            """,
            (
                folder_id, uid, message_id, int(_SEEN in flags), int(_FLAGGED in flags),
                _flags_text(flags),
            ),
        )  # fmt: skip

    def _hydrate(self, ids: Sequence[int]) -> list[StoredMessage]:
        if not ids:
            return []
        rows = self._conn.execute(_MESSAGE_ROWS, {"ids": _json(ids)}).fetchall()
        attachments: dict[int, list[AttachmentInfo]] = {}
        for row in self._conn.execute(
            "SELECT * FROM attachments WHERE message_id IN"
            " (SELECT value FROM json_each(?)) ORDER BY message_id, part",
            (_json(ids),),
        ):
            attachments.setdefault(row["message_id"], []).append(
                AttachmentInfo(
                    part=row["part"],
                    filename=row["filename"],
                    content_type=row["content_type"],
                    size=row["size"],
                    encoding=row["encoding"],
                )
            )
        known = self.known_addresses([row["from_address"] for row in rows])
        by_id = {
            row["id"]: StoredMessage(
                id=row["id"],
                parsed=_parsed(row, tuple(attachments.get(row["id"], []))),
                folders=tuple(sorted(json.loads(row["folders"]))),
                unread=bool(row["unread"]),
                flagged=bool(row["flagged_any"]),
                known_sender=row["from_address"] in known,
            )
            for row in rows
        }
        return [by_id[i] for i in ids if i in by_id]


_INSERT_MESSAGE: Final = """
    INSERT INTO messages (
        identity, message_id, date, from_name, from_address, to_addrs, cc_addrs,
        sender_text, recipients_text, subject, body, text_truncated,
        hidden_content_removed, auth_spf, auth_dkim, auth_dmarc, in_reply_to, refs,
        parse_state, size, has_attachments
    ) VALUES (
        :identity, :message_id, :date, :from_name, :from_address, :to_addrs, :cc_addrs,
        :sender_text, :recipients_text, :subject, :body, :text_truncated,
        :hidden_content_removed, :auth_spf, :auth_dkim, :auth_dmarc, :in_reply_to,
        :refs, :parse_state, :size, :has_attachments
    ) ON CONFLICT (identity) DO NOTHING
"""


def _message_values(parsed: ParsedMessage) -> dict[str, Any]:
    return {
        "identity": parsed.identity,
        "message_id": parsed.message_id,
        "date": int(parsed.date.timestamp()),
        "from_name": parsed.from_name,
        "from_address": parsed.from_address,
        "to_addrs": "\n".join(parsed.to),
        "cc_addrs": "\n".join(parsed.cc),
        "sender_text": f"{parsed.from_name} {parsed.from_address}".strip(),
        "recipients_text": " ".join((*parsed.to, *parsed.cc)),
        "subject": parsed.subject,
        "body": parsed.body_text,
        "text_truncated": int(parsed.text_truncated),
        "hidden_content_removed": int(parsed.hidden_content_removed),
        "auth_spf": parsed.auth.spf.value,
        "auth_dkim": parsed.auth.dkim.value,
        "auth_dmarc": parsed.auth.dmarc.value,
        "in_reply_to": parsed.in_reply_to,
        "refs": " ".join(parsed.references),
        "parse_state": parsed.parse_state.value,
        "size": parsed.size,
        "has_attachments": int(bool(parsed.attachments)),
    }


def _parsed(
    row: Mapping[str, Any], attachments: tuple[AttachmentInfo, ...]
) -> ParsedMessage:
    return ParsedMessage(
        identity=row["identity"],
        message_id=row["message_id"],
        date=datetime.fromtimestamp(row["date"], UTC),
        from_name=row["from_name"],
        from_address=row["from_address"],
        to=tuple(a for a in row["to_addrs"].split("\n") if a),
        cc=tuple(a for a in row["cc_addrs"].split("\n") if a),
        subject=row["subject"],
        body_text=row["body"],
        text_truncated=bool(row["text_truncated"]),
        hidden_content_removed=bool(row["hidden_content_removed"]),
        auth=AuthResults(
            spf=_verdict(row["auth_spf"]),
            dkim=_verdict(row["auth_dkim"]),
            dmarc=_verdict(row["auth_dmarc"]),
        ),
        in_reply_to=row["in_reply_to"],
        references=tuple(row["refs"].split()),
        parse_state=ParseState(row["parse_state"]),
        size=row["size"],
        attachments=attachments,
    )


def _flags_text(flags: frozenset[str]) -> str:
    return " ".join(sorted(flags))


def _clip(snippet: str | None) -> str:
    text = " ".join((snippet or "").split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 3] + "..."
