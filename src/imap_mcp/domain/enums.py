"""Enumerations shared by every layer of imap-mcp."""

from __future__ import annotations

from enum import StrEnum


class FolderRole(StrEnum):
    """The role of a mail folder.

    iCloud advertises no SPECIAL-USE, so roles also come from folder names.
    """

    INBOX = "inbox"
    DRAFTS = "drafts"
    SENT = "sent"
    JUNK = "junk"
    TRASH = "trash"
    ARCHIVE = "archive"
    OTHER = "other"


class SyncStateKind(StrEnum):
    """The state of the most recent sync, as the agent sees it."""

    NEVER = "never"
    COMPLETE = "complete"
    PARTIAL = "partial"
    IN_PROGRESS_ELSEWHERE = "in_progress_elsewhere"
    FAILED = "failed"


class ParseState(StrEnum):
    """Whether the parser could extract the text of a message."""

    OK = "ok"
    FAILED = "parse_failed"


class AuthVerdict(StrEnum):
    """One result from an `Authentication-Results` header (RFC 8601)."""

    PASS = "pass"  # noqa: S105 - a verdict name, not a password
    FAIL = "fail"
    SOFTFAIL = "softfail"
    NEUTRAL = "neutral"
    NONE = "none"
    TEMPERROR = "temperror"
    PERMERROR = "permerror"
    POLICY = "policy"
    UNKNOWN = "unknown"
