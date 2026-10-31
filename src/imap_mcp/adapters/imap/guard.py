"""The IMAP command allowlist (design doc, Security T1).

IMAP makes a client destructive by accident: `SELECT` then `CLOSE` expunges,
and a plain `BODY[]` fetch sets `\\Seen`. This module checks every command at
the two places where imapclient hands a command to the wire, before the
command is sent. A command outside the allowlist raises `GuardViolation`.

The test IMAP server imports the same constants, so the test check and the
production check cannot drift apart.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import structlog

from imap_mcp.domain.errors import GuardViolation

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

log = structlog.get_logger(__name__)

ALLOWED_COMMANDS: Final = frozenset(
    {
        "APPEND", "CAPABILITY", "EXAMINE", "ID", "LIST",
        "LOGIN", "LOGOUT", "NOOP", "STATUS",
    }
)  # fmt: skip
"""Commands allowed without the UID prefix."""

ALLOWED_UID_COMMANDS: Final = frozenset({"FETCH", "SEARCH"})
"""Commands allowed after `UID`."""

DRAFT_FLAG: Final = "\\DRAFT"

# A fetch item that sets \Seen: BODY[...] or BINARY[...] without .PEEK, and the
# RFC822 and RFC822.TEXT items. RFC822.SIZE and RFC822.HEADER do not set \Seen.
_SEEN_SETTING_ITEM: Final = re.compile(
    r"(?<![.\w])(?:BODY|BINARY)\[|(?<![.\w])RFC822(?:\.TEXT)?(?![.\w])"
)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _refuse(command: str, reason: str) -> GuardViolation:
    # The command name is safe to log. Arguments can hold folder names.
    log.error("imap_command_refused", command=command, reason=reason)
    return GuardViolation(f"IMAP command {command} refused: {reason}")


def check_fetch_items(items: str) -> None:
    """Refuse fetch items that set `\\Seen`.

    Args:
        items: The fetch item list as sent, for example `(UID BODY.PEEK[])`.

    Raises:
        GuardViolation: If an item sets `\\Seen`.
    """
    if _SEEN_SETTING_ITEM.search(items.upper()):
        raise _refuse("UID FETCH", "the fetch item sets \\Seen; use BODY.PEEK")


def check_command(name: str, args: Sequence[object], drafts_folder: str) -> None:
    """Check one command that goes through `imaplib.IMAP4._command`.

    Args:
        name: The command name, for example `UID` or `EXAMINE`.
        args: The command arguments as imaplib receives them.
        drafts_folder: The only folder `APPEND` can target, in the quoted
            wire form that imapclient sends. Empty means no `APPEND` at all.

    Raises:
        GuardViolation: If the command is outside the allowlist.
    """
    verb = name.upper()
    if verb == "UID":
        sub = _text(args[0]).upper() if args else ""
        if sub not in ALLOWED_UID_COMMANDS:
            raise _refuse(f"UID {sub}", "not in the allowlist")
        if sub == "FETCH":
            check_fetch_items(" ".join(_text(a) for a in args[2:]))
        return
    if verb not in ALLOWED_COMMANDS:
        raise _refuse(verb, "not in the allowlist")
    if verb == "APPEND":
        target = _text(args[0]) if args else ""
        if not drafts_folder or target != drafts_folder:
            raise _refuse(verb, "APPEND can target only the Drafts folder")
        flags = _text(args[1]).upper() if len(args) > 1 else ""
        if DRAFT_FLAG not in flags:
            raise _refuse(verb, "APPEND must set the \\Draft flag")


def check_raw_command(command: bytes, *, uid: bool) -> None:
    """Check one command that imapclient sends with `_raw_command`.

    Args:
        command: The command name, for example `b"SEARCH"`.
        uid: True if imapclient adds the `UID` prefix.

    Raises:
        GuardViolation: If the command is outside the allowlist.
    """
    verb = command.decode("ascii", "replace").upper()
    if uid and verb in ALLOWED_UID_COMMANDS and verb != "FETCH":
        return
    shown = f"UID {verb}" if uid else verb
    raise _refuse(shown, "not in the allowlist")


class GuardHandle:
    """Holds the Drafts folder that the installed guard allows for `APPEND`."""

    def __init__(self, normalise: Callable[[str], object]) -> None:
        """Create the handle.

        Args:
            normalise: Turns a folder name into the quoted wire form.
        """
        self._normalise = normalise
        self.wire_drafts = ""

    def allow_drafts_folder(self, name: str) -> None:
        """Allow `APPEND` to this folder only.

        Args:
            name: The Drafts folder name.
        """
        self.wire_drafts = _text(self._normalise(name)) if name else ""


def install_guard(client: Any) -> GuardHandle:
    """Wrap the two command paths of one `IMAPClient` instance.

    The wrappers replace attributes on this instance only. They run before any
    byte reaches the socket, so a refused command is never sent. `APPEND` is
    refused until the caller names the Drafts folder on the returned handle.

    Args:
        client: A connected `imapclient.IMAPClient`.

    Returns:
        The handle that sets the allowed Drafts folder.
    """
    # imapclient offers no public hook. These two private attributes are the
    # only places that see every command (checked against imapclient 4.1.0).
    imap = client._imap
    handle = GuardHandle(client._normalise_folder)
    original_command: Callable[..., Any] = imap._command
    original_raw: Callable[..., Any] = client._raw_command

    def guarded_command(name: str, *args: object) -> Any:
        check_command(name, args, handle.wire_drafts)
        return original_command(name, *args)

    def guarded_raw(command: bytes, args: object, *rest: Any, **kwargs: Any) -> Any:
        # Same signature as IMAPClient._raw_command(command, args, uid=True).
        uid = bool(rest[0] if rest else kwargs.get("uid", True))
        check_raw_command(command, uid=uid and bool(client.use_uid))
        return original_raw(command, args, *rest, **kwargs)

    imap._command = guarded_command
    client._raw_command = guarded_raw
    return handle
