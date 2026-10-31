"""Exceptions that cross layer boundaries.

Adapters translate every library error into one of these types, so the core
and the entry points catch a small, fixed set.
"""

from __future__ import annotations


class ImapMcpError(Exception):
    """The base class of every imap-mcp error."""


class ConfigError(ImapMcpError):
    """The config file is missing, invalid or unsafe."""


class CredentialError(ImapMcpError):
    """The credential store could not supply the password."""


class MailError(ImapMcpError):
    """An IMAP operation failed: connection, login, protocol or parse."""


class GuardViolation(MailError):
    """Code tried to send an IMAP command outside the allowlist.

    This error always means a bug in imap-mcp. It is never retried.
    """


class NotFoundError(ImapMcpError):
    """A message, part or folder that a caller named does not exist."""


class InvalidRequestError(ImapMcpError):
    """A caller passed a value that the tool cannot accept."""
