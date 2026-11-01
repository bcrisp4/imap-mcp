"""Logging to stderr with structlog.

stdout carries the MCP protocol, so every log line goes to stderr. Logs must be
safe to paste into a public issue, so a processor removes any value under a key
that can hold mail content or a secret.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from typing import Any

REDACTED: Final = "[redacted]"

# Keys that can carry content, addresses or secrets. Code must not log these.
# This list is a second line of defence for a mistake.
SENSITIVE_KEYS: Final = frozenset(
    {
        "address", "addresses", "body", "cc", "filename", "from", "from_address",
        "password", "query", "recipients", "snippet", "subject", "text", "to",
    }
)  # fmt: skip


def redact_sensitive(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the value of each sensitive key.

    Args:
        _logger: Unused. Part of the structlog processor signature.
        _method: Unused. Part of the structlog processor signature.
        event_dict: The log event.

    Returns:
        The event with sensitive values replaced.
    """
    for key in SENSITIVE_KEYS.intersection(event_dict):
        event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """Send structlog output to stderr as key=value lines.

    Args:
        level: The minimum level to emit.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "event"]
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Resolve sys.stderr at each call: `imap-mcp sync` swaps it for a proxy
        # that prints above the progress bar.
        logger_factory=lambda *_: structlog.PrintLogger(file=sys.stderr),
    )
