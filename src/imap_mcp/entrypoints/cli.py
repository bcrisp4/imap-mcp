"""The `imap-mcp` command line.

stdout carries the MCP protocol under `serve`, so every human-readable line
goes to stderr. Output never contains message content.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import traceback
from email import message_from_bytes, policy
from typing import TYPE_CHECKING, Final

from imap_mcp.adapters.flock import FileLock
from imap_mcp.adapters.keychain import KeychainCredentialSource
from imap_mcp.adapters.sqlite.connection import has_other_connections
from imap_mcp.bootstrap import build_services, build_sources, lock_path, open_index
from imap_mcp.config import load_config
from imap_mcp.domain.enums import FolderRole, SyncStateKind
from imap_mcp.domain.errors import ImapMcpError
from imap_mcp.log import configure_logging

if TYPE_CHECKING:
    from collections.abc import Sequence

    from imap_mcp.config import Config
    from imap_mcp.ports import MailSource

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_BUSY: Final = 2
EXIT_INTERRUPTED: Final = 130
DEBUG_ENV: Final = "IMAP_MCP_DEBUG"


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_serve(config: Config) -> int:
    """Run the MCP server on stdio."""
    from imap_mcp.entrypoints.mcp.app import build_server

    build_server(build_services(config)).run()
    return EXIT_OK


def cmd_set_password(config: Config) -> int:
    """Store the IMAP app password in the Keychain."""
    _say(f"Storing the app password for {config.username} in the login Keychain.")
    KeychainCredentialSource(config.username).store_password()
    _say("Stored. When macOS asks, choose Always Allow for /usr/bin/security.")
    return EXIT_OK


def cmd_sync(config: Config) -> int:
    """Run a full sync with no time limit.

    A terminal gets a live view. Other output, such as a log file, gets
    structlog events.
    """
    from rich.console import Console

    from imap_mcp.entrypoints.sync_progress import (
        LogProgress,
        TerminalProgress,
        duration,
    )

    started = time.monotonic()
    console = Console(stderr=True)
    if console.is_terminal:
        with TerminalProgress(console, config.username) as view:
            report = build_services(config, progress=view).sync.full_sync()
    else:
        report = build_services(config, progress=LogProgress()).sync.full_sync()
    if report.kind is SyncStateKind.IN_PROGRESS_ELSEWHERE:
        _say("Another imap-mcp process is syncing. Try again later.")
        return EXIT_BUSY
    if report.kind is SyncStateKind.FAILED:
        _say(f"Sync failed: {report.error}")
        return EXIT_ERROR
    _say(
        f"{'✓ ' if report.kind is SyncStateKind.COMPLETE else ''}"
        f"Sync {report.kind.value} in {duration(time.monotonic() - started)}:"
        f" {report.new_messages:,} new, {report.linked:,} linked,"
        f" {report.removed:,} removed, {report.flags_updated:,} flag updates."
    )
    return EXIT_OK


def cmd_status(config: Config) -> int:
    """Show sync state, folder counts and database size."""
    lock = FileLock(lock_path(config))
    index = open_index(config, lock)
    state = index.sync_state()
    stats = index.stats()
    _say(f"Database: {config.db_path}")
    _say(f"Size: {_size(stats.file_bytes)} ({_size(stats.free_bytes)} free pages)")
    _say(f"Messages: {stats.messages}")
    _say(f"Last sync: {state.kind.value}, success {state.last_success or 'never'}")
    if state.error:
        _say(f"Last error: {state.error}")
    folders = index.list_folders()
    index.close()
    for folder in folders:
        _say(f"  {folder.name} [{folder.role.value}]: {folder.messages}"
             f" messages, {folder.unread} unread")  # fmt: skip
    return EXIT_OK


def cmd_probe(config: Config) -> int:
    """Log in and report server capabilities and folders (design O1, O2)."""
    with build_sources(config).open() as source:
        _say("Capabilities: " + " ".join(sorted(source.capabilities())))
        folders = source.list_folders()
        for folder in folders:
            status = source.status(folder.name)
            _say(f"  {folder.name!r} [{folder.role.value}] uidvalidity="
                 f"{status.uidvalidity} messages={status.messages}")  # fmt: skip
        inbox = next((f for f in folders if f.role is FolderRole.INBOX), None)
        if inbox is None:
            return EXIT_OK
        source.examine(inbox.name)
        uids = source.search_uids()
        if not uids:
            _say("INBOX is empty, so the message checks were skipped.")
            return EXIT_OK
        (header,) = source.fetch_headers([uids[-1]])
        _say(f"BODYSTRUCTURE parsed: {header.structure is not None}")
        _say(f"HEADER Message-ID search: {_message_id_search(source, header.header)}")
    return EXIT_OK


def _message_id_search(source: MailSource, header: bytes) -> str:
    message_id = message_from_bytes(header, policy=policy.compat32).get("Message-ID")
    if not message_id:
        return "skipped (no Message-ID)"
    return "works" if source.search_message_id(str(message_id)) else "found nothing"


def cmd_vacuum(config: Config) -> int:
    """Rebuild the database file and merge the full-text index."""
    lock = FileLock(lock_path(config))
    index = open_index(config, lock)
    size = index.stats().file_bytes
    free = shutil.disk_usage(config.db_path.parent).free
    if free < 2 * size:
        _say(f"Need {_size(2 * size)} free disk space, have {_size(free)}.")
        return EXIT_ERROR
    if not lock.try_acquire():
        _say("Another imap-mcp process is syncing. Try again later.")
        return EXIT_BUSY
    try:
        index.vacuum()
    finally:
        lock.release()
    after = index.stats().file_bytes
    index.close()
    _say(f"Vacuumed: {_size(size)} -> {_size(after)}")
    return EXIT_OK


def cmd_purge(config: Config, *, confirmed: bool) -> int:
    """Delete the index files."""
    if not confirmed:
        _say(f"This deletes {config.db_path} and its files. Run again with --yes.")
        return EXIT_ERROR
    lock = FileLock(lock_path(config))
    if not lock.try_acquire():
        _say("Another imap-mcp process is syncing. Try again later.")
        return EXIT_BUSY
    try:
        if has_other_connections(config.db_path):
            _say("Another imap-mcp process has the index open. Quit those agents.")
            return EXIT_BUSY
        for suffix in ("", "-wal", "-shm"):
            config.db_path.with_name(config.db_path.name + suffix).unlink(
                missing_ok=True
            )
    finally:
        lock.close()
    lock_path(config).unlink(missing_ok=True)
    _say("Index deleted. The next sync rebuilds it.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: The arguments, without the program name.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(
        prog="imap-mcp", description="Read-only MCP server for one IMAP mailbox."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run the MCP server on stdio.")
    sub.add_parser("set-password", help="Store the app password in the Keychain.")
    sub.add_parser("sync", help="Run a full sync with no time limit.")
    sub.add_parser("status", help="Show sync state and database size.")
    sub.add_parser("probe", help="Report server capabilities and folders.")
    sub.add_parser("vacuum", help="Rebuild the database file.")
    purge = sub.add_parser("purge", help="Delete the index.")
    purge.add_argument("--yes", action="store_true", help="Confirm the deletion.")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        config = load_config(os.environ)
        match args.command:
            case "serve":
                return cmd_serve(config)
            case "set-password":
                return cmd_set_password(config)
            case "sync":
                return cmd_sync(config)
            case "status":
                return cmd_status(config)
            case "probe":
                return cmd_probe(config)
            case "vacuum":
                return cmd_vacuum(config)
            case _:
                return cmd_purge(config, confirmed=bool(args.yes))
    except ImapMcpError as exc:
        _say(f"imap-mcp: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _say("imap-mcp: interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:
        # The exception message is left out, because library error text can
        # quote mail content. OSError.strerror is OS text, so it is safe.
        # The traceback frames show where it failed.
        reason = (
            f": {exc.strerror}" if isinstance(exc, OSError) and exc.strerror else ""
        )
        _say(f"imap-mcp: unexpected {type(exc).__name__}{reason}")
        if os.environ.get(DEBUG_ENV):
            _say("".join(traceback.format_tb(exc.__traceback__)).rstrip())
        else:
            _say(f"Set {DEBUG_ENV}=1 to show where it failed.")
        return EXIT_ERROR
