"""Immutable data types that pass between the layers of imap-mcp.

Times are timezone-aware UTC `datetime` values in this layer. The index stores
them as Unix integer seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from imap_mcp.domain.enums import AuthVerdict, FolderRole, ParseState, SyncStateKind

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class Folder:
    """A folder as `LIST` reports it.

    Attributes:
        name: The folder name, decoded from modified UTF-7.
        role: The role from SPECIAL-USE, or from the name.
    """

    name: str
    role: FolderRole


@dataclass(frozen=True, slots=True)
class FolderStatus:
    """The `STATUS` values that decide whether a folder changed.

    Attributes:
        messages: The number of messages in the folder.
        uidnext: The next UID the server will assign.
        uidvalidity: The UIDVALIDITY value. A change invalidates every UID.
        unseen: The number of messages without `\\Seen`.
        highestmodseq: The CONDSTORE mod-sequence, or None without CONDSTORE.
    """

    messages: int
    uidnext: int
    uidvalidity: int
    unseen: int
    highestmodseq: int | None = None


@dataclass(frozen=True, slots=True)
class TextPart:
    """One text part of a message, from `BODYSTRUCTURE`.

    Attributes:
        part: The IMAP part number, for example "1" or "1.2".
        subtype: "plain" or "html".
        charset: The declared charset, or None.
        encoding: The content transfer encoding, lower case.
        size: The encoded size in bytes.
    """

    part: str
    subtype: str
    charset: str | None
    encoding: str
    size: int


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """One attachment of a message. The index stores no attachment bytes.

    Attributes:
        part: The IMAP part number.
        filename: The file name the sender gave. Untrusted.
        content_type: The MIME type, lower case.
        size: The encoded size in bytes.
        encoding: The content transfer encoding, lower case.
    """

    part: str
    filename: str | None
    content_type: str
    size: int
    encoding: str = "7bit"


@dataclass(frozen=True, slots=True)
class MessageStructure:
    """The parts of a message that matter to imap-mcp.

    Attributes:
        text_parts: The parts to index, in display order.
        attachments: The parts to list as attachments.
    """

    text_parts: tuple[TextPart, ...] = ()
    attachments: tuple[AttachmentInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchedHeader:
    """What the first fetch of a new message returns.

    Attributes:
        uid: The UID in the folder that was fetched.
        flags: The IMAP flags, for example `\\Seen`.
        internal_date: The server's receive time.
        size: The RFC822 size in bytes.
        header: The raw header block.
        structure: The parsed `BODYSTRUCTURE`, or None if it was missing or bad.
    """

    uid: int
    flags: frozenset[str]
    internal_date: datetime
    size: int
    header: bytes
    structure: MessageStructure | None


@dataclass(frozen=True, slots=True)
class AuthResults:
    """Sender authentication results that the receiving server recorded."""

    spf: AuthVerdict = AuthVerdict.UNKNOWN
    dkim: AuthVerdict = AuthVerdict.UNKNOWN
    dmarc: AuthVerdict = AuthVerdict.UNKNOWN


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """A message after parsing, ready for the index.

    Every text field except `identity` comes from the sender and is untrusted.

    Attributes:
        identity: The key that joins copies of one message in many folders.
        message_id: The `Message-ID` header, or None.
        date: The `Date` header, or the internal date if `Date` is bad.
        from_name: The display name of the sender.
        from_address: The address of the sender.
        to: The `To` addresses.
        cc: The `Cc` addresses.
        subject: The subject.
        body_text: The plain text to index and show.
        text_truncated: True if the text fetch stopped at the size cap.
        hidden_content_removed: True if the HTML held hidden elements.
        auth: The authentication results.
        in_reply_to: The `In-Reply-To` header, or None.
        references: The `References` message IDs.
        parse_state: Whether the text parse succeeded.
        size: The RFC822 size in bytes.
        attachments: The attachments.
    """

    identity: str
    message_id: str | None
    date: datetime
    from_name: str
    from_address: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body_text: str
    text_truncated: bool
    hidden_content_removed: bool
    auth: AuthResults
    in_reply_to: str | None
    references: tuple[str, ...]
    parse_state: ParseState
    size: int
    attachments: tuple[AttachmentInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A message as the index returns it.

    Attributes:
        id: The local message ID. Valid until the index is rebuilt.
        parsed: The parsed content.
        folders: The folders that hold a copy.
        unread: True if no copy has `\\Seen`.
        flagged: True if any copy has `\\Flagged`.
        known_sender: True if I sent mail to the sender's address.
    """

    id: int
    parsed: ParsedMessage
    folders: tuple[str, ...]
    unread: bool
    flagged: bool
    known_sender: bool


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """The filters of one search. None means "no filter".

    Attributes:
        text: Keywords, or None to list by date.
        folder: Limit to one folder. Junk and Trash need this to appear.
        from_address: A substring of the sender address or name.
        to_address: A substring of a `To` or `Cc` address.
        since: The earliest date, inclusive.
        before: The latest date, exclusive.
        unread: Filter on the unread state.
        flagged: Filter on the flagged state.
        has_attachment: Filter on attachments.
        limit: The page size.
        offset: The page start.
    """

    text: str | None = None
    folder: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    since: datetime | None = None
    before: datetime | None = None
    unread: bool | None = None
    flagged: bool | None = None
    has_attachment: bool | None = None
    limit: int = 10
    offset: int = 0


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result.

    Attributes:
        message: The message.
        snippet: Text around the match, with matched terms in brackets.
    """

    message: StoredMessage
    snippet: str


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of search results.

    Attributes:
        total: The number of matches over all pages.
        hits: The matches on this page.
    """

    total: int
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True, slots=True)
class FolderSummary:
    """A folder with its counts from the index."""

    name: str
    role: FolderRole
    messages: int
    unread: int


@dataclass(frozen=True, slots=True)
class SyncState:
    """The shared record of sync progress that every process reads.

    Attributes:
        last_success: The end time of the last complete sync.
        last_attempt: The start time of the last sync of any outcome.
        kind: The outcome of the last attempt.
        error: The error of the last failed attempt.
        remaining: Messages left to fetch after a partial sync. None means
            unknown, because folders were left unvisited.
    """

    last_success: datetime | None = None
    last_attempt: datetime | None = None
    kind: SyncStateKind = SyncStateKind.NEVER
    error: str | None = None
    remaining: int | None = 0


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync run did.

    Attributes:
        kind: The outcome.
        new_messages: Messages fetched and indexed.
        linked: Locations added for messages already in the index.
        removed: Locations deleted because the server no longer has them.
        flags_updated: Locations whose flags changed.
        remaining: Messages left to fetch when the time budget expired. None
            means unknown, because folders were left unvisited.
        error: The error message of a failed sync.
        folders: The folders the run examined.
    """

    kind: SyncStateKind
    new_messages: int = 0
    linked: int = 0
    removed: int = 0
    flags_updated: int = 0
    remaining: int | None = 0
    error: str | None = None
    folders: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class DraftResult:
    """What `create_draft` returns.

    Attributes:
        message_id: The `Message-ID` of the new draft.
        folder: The Drafts folder name.
        new_recipients: Recipients I never wrote to before.
    """

    message_id: str
    folder: str
    new_recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndexStats:
    """Size figures for `imap-mcp status`.

    Attributes:
        messages: Messages in the index.
        file_bytes: The database file size.
        free_bytes: Bytes in free pages.
    """

    messages: int
    file_bytes: int
    free_bytes: int
