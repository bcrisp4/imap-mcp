"""Opening the SQLite database with the settings in design Appendix B."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

# Applied on every connection. `journal_mode` persists in the file, and
# repeating it is harmless. `synchronous = NORMAL` can lose the last commit on
# power loss, which is acceptable because a sync rebuilds the index.
# `cache_size` is 16 MiB because every agent process holds its own cache.
_PRAGMAS: Final = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA cache_size = -16000",
    "PRAGMA secure_delete = ON",
)
_DISABLE: Final = False
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


def open_database(path: Path) -> sqlite3.Connection:
    """Open (and create if needed) the index database.

    The directory gets mode 0700 and the database files mode 0600, because the
    index holds the text of private mail.

    Args:
        path: The database file.

    Returns:
        A connection in autocommit mode. Write transactions use explicit
        `BEGIN IMMEDIATE` (see `write_transaction`). The connection can move
        between threads, but callers must serialise its use.
    """
    path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    path.parent.chmod(_DIR_MODE)
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    # A misspelt column in double quotes must be an error, not a string.
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, _DISABLE)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, _DISABLE)
    if int(conn.execute("PRAGMA page_count").fetchone()[0]) == 0:
        # auto_vacuum takes effect only in a new, empty file, before the first
        # table and before the switch to WAL writes the file header.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    # A read in WAL mode creates the -wal and -shm files now, so the chmod
    # below covers them. Created later, they would get the process umask.
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.chmod(_FILE_MODE)
    return conn


def has_other_connections(path: Path) -> bool:
    """Return True if another connection has the database open.

    Leaving WAL mode needs the only connection to the file, so a refused switch
    means another process (an idle MCP server, for example) still has it open.
    A switch that succeeds is undone at once, so the file stays in WAL mode.

    Args:
        path: The database file.

    Returns:
        True if another connection holds the file open.
    """
    if not path.exists():
        return False
    conn = sqlite3.connect(path, autocommit=True, timeout=0)
    try:
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if str(mode).lower() == "delete":
            conn.execute("PRAGMA journal_mode = WAL")
            return False
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()
    return True


@contextlib.contextmanager
def read_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run several reads against one snapshot of the database.

    In WAL mode, each autocommit statement can see a different commit. A
    deferred transaction pins one snapshot for every read inside it.

    Args:
        conn: An autocommit connection.

    Yields:
        The same connection.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextlib.contextmanager
def write_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run a block in one `BEGIN IMMEDIATE` transaction.

    `IMMEDIATE` takes the write lock up front, so `busy_timeout` governs the
    wait. A deferred transaction that reads first and then writes can fail with
    a busy error that the timeout cannot retry.

    Args:
        conn: An autocommit connection.

    Yields:
        The same connection.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
