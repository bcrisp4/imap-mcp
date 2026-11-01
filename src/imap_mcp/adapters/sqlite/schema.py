"""The index schema and its migrations, versioned by `PRAGMA user_version`.

Migrations run at startup while the process holds the sync lock, so two
processes that start together cannot migrate at the same time.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from imap_mcp.adapters.sqlite.connection import write_transaction

_V1: Final = """
CREATE TABLE folders (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    role          TEXT    NOT NULL,
    synced        INTEGER NOT NULL DEFAULT 0 CHECK (synced IN (0, 1)),
    uidvalidity   INTEGER,
    uidnext       INTEGER,
    messages      INTEGER,
    unseen        INTEGER,
    highestmodseq INTEGER
) STRICT;

CREATE INDEX folders_role ON folders (role);

CREATE TABLE messages (
    id                     INTEGER PRIMARY KEY,
    identity               TEXT    NOT NULL UNIQUE,
    message_id             TEXT,
    date                   INTEGER NOT NULL,
    from_name              TEXT    NOT NULL,
    from_address           TEXT    NOT NULL,
    to_addrs               TEXT    NOT NULL,
    cc_addrs               TEXT    NOT NULL,
    sender_text            TEXT    NOT NULL,
    recipients_text        TEXT    NOT NULL,
    subject                TEXT    NOT NULL,
    body                   TEXT    NOT NULL,
    text_truncated         INTEGER NOT NULL CHECK (text_truncated IN (0, 1)),
    hidden_content_removed INTEGER NOT NULL CHECK (hidden_content_removed IN (0, 1)),
    auth_spf               TEXT    NOT NULL,
    auth_dkim              TEXT    NOT NULL,
    auth_dmarc             TEXT    NOT NULL,
    in_reply_to            TEXT,
    refs                   TEXT    NOT NULL,
    parse_state            TEXT    NOT NULL,
    size                   INTEGER NOT NULL,
    has_attachments        INTEGER NOT NULL CHECK (has_attachments IN (0, 1))
) STRICT;

CREATE INDEX messages_date ON messages (date);
CREATE INDEX messages_message_id ON messages (message_id);

CREATE TABLE locations (
    folder_id  INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
    uid        INTEGER NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    seen       INTEGER NOT NULL CHECK (seen IN (0, 1)),
    flagged    INTEGER NOT NULL CHECK (flagged IN (0, 1)),
    flags      TEXT    NOT NULL,
    PRIMARY KEY (folder_id, uid)
) STRICT, WITHOUT ROWID;

CREATE INDEX locations_message ON locations (message_id);

CREATE TABLE attachments (
    message_id   INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    part         TEXT    NOT NULL,
    filename     TEXT,
    content_type TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    encoding     TEXT    NOT NULL,
    PRIMARY KEY (message_id, part)
) STRICT, WITHOUT ROWID;

CREATE TABLE sync_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_success INTEGER,
    last_attempt INTEGER,
    kind         TEXT    NOT NULL,
    error        TEXT,
    remaining    INTEGER NOT NULL
) STRICT;

CREATE VIRTUAL TABLE messages_fts USING fts5 (
    subject, sender_text, recipients_text, body,
    content = 'messages', content_rowid = 'id',
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, subject, sender_text, recipients_text, body)
    VALUES (new.id, new.subject, new.sender_text, new.recipients_text, new.body);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (
        messages_fts, rowid, subject, sender_text, recipients_text, body
    ) VALUES (
        'delete', old.id, old.subject, old.sender_text, old.recipients_text, old.body
    );
END;

CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts (
        messages_fts, rowid, subject, sender_text, recipients_text, body
    ) VALUES (
        'delete', old.id, old.subject, old.sender_text, old.recipients_text, old.body
    );
    INSERT INTO messages_fts (rowid, subject, sender_text, recipients_text, body)
    VALUES (new.id, new.subject, new.sender_text, new.recipients_text, new.body);
END;
"""

# v2: `remaining` can be NULL (unknown). The table holds one row of derived
# state, so it is rebuilt. The next tool call then syncs.
_V2: Final = """
DROP TABLE sync_state;

CREATE TABLE sync_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_success INTEGER,
    last_attempt INTEGER,
    kind         TEXT    NOT NULL,
    error        TEXT,
    remaining    INTEGER
) STRICT;
"""

# v3: the identity key now covers the message size, and auth verdicts come
# from a stricter parser. Rows stored under the old rules would not link to
# new copies and would keep old verdicts, so the index is cleared. The next
# sync downloads every message again.
_V3: Final = """
DELETE FROM messages;

UPDATE folders SET synced = 0, uidvalidity = NULL, uidnext = NULL,
    messages = NULL, unseen = NULL, highestmodseq = NULL;

DELETE FROM sync_state;
"""

MIGRATIONS: Final = (_V1, _V2, _V3)
"""Migration N takes the database from `user_version` N to N + 1."""


def needs_migration(conn: sqlite3.Connection) -> bool:
    """Return True if the schema is older than this code."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0]) < len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema up to date.

    The caller must hold the sync lock.

    Args:
        conn: An autocommit connection.

    Returns:
        The schema version after migration.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with write_transaction(conn):
            for statement in _statements(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target:d}")
    return len(MIGRATIONS)


def _statements(script: str) -> list[str]:
    """Split a script on `;` at line ends, keeping trigger bodies whole."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.strip().splitlines():
        current.append(line)
        text = "\n".join(current).strip()
        if text.endswith(";") and sqlite3.complete_statement(text):
            statements.append(text)
            current = []
    return statements
