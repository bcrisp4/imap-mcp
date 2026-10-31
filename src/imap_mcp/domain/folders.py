"""Folder role rules.

iCloud advertises no SPECIAL-USE (RFC 6154), so a role comes from the
SPECIAL-USE flag when the server sends one, and from the folder name when it
does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from imap_mcp.domain.enums import FolderRole

if TYPE_CHECKING:
    from collections.abc import Iterable

_FLAG_ROLES: Final = {
    "\\DRAFTS": FolderRole.DRAFTS,
    "\\SENT": FolderRole.SENT,
    "\\JUNK": FolderRole.JUNK,
    "\\TRASH": FolderRole.TRASH,
    "\\ARCHIVE": FolderRole.ARCHIVE,
}

_NAME_ROLES: Final = {
    "inbox": FolderRole.INBOX,
    "drafts": FolderRole.DRAFTS,
    "draft": FolderRole.DRAFTS,
    "sent messages": FolderRole.SENT,
    "sent": FolderRole.SENT,
    "sent items": FolderRole.SENT,
    "sent mail": FolderRole.SENT,
    "junk": FolderRole.JUNK,
    "spam": FolderRole.JUNK,
    "junk e-mail": FolderRole.JUNK,
    "deleted messages": FolderRole.TRASH,
    "trash": FolderRole.TRASH,
    "deleted items": FolderRole.TRASH,
    "bin": FolderRole.TRASH,
    "archive": FolderRole.ARCHIVE,
}

EXCLUDED_FROM_SEARCH: Final = frozenset({FolderRole.JUNK, FolderRole.TRASH})
"""Roles that a search skips unless the agent names the folder (design C10)."""


def folder_role(name: str, flags: Iterable[str]) -> FolderRole:
    """Return the role of a folder.

    Args:
        name: The folder name.
        flags: The LIST attributes, for example `\\Drafts`.

    Returns:
        The role from a SPECIAL-USE flag, or from the name, or OTHER.
    """
    if name.upper() == "INBOX":
        return FolderRole.INBOX
    for flag in flags:
        role = _FLAG_ROLES.get(flag.upper())
        if role is not None:
            return role
    return _NAME_ROLES.get(name.casefold(), FolderRole.OTHER)
