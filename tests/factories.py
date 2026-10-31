"""Factories that build raw RFC 5322 messages for tests."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

from imap_mcp.domain.content.parse import parse_message
from imap_mcp.domain.model import FetchedHeader, ParsedMessage

_counter = iter(range(1, 1_000_000))


def make_message(
    *,
    subject: str = "Your car insurance renewal",
    sender: str = "Example Insurer <renewals@example-insurer.co.uk>",
    to: str = "ben@thecrisp.io",
    cc: str | None = None,
    date: datetime | None = None,
    message_id: str | None = None,
    body: str | None = "Your policy renews on 1 March 2027.",
    html: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    headers: dict[str, str] | None = None,
    **overrides: Any,
) -> bytes:
    """Build a message.

    Args:
        subject: The subject.
        sender: The From header.
        to: The To header.
        cc: The Cc header.
        date: The Date header. Defaults to 2026-02-14 09:12 UTC.
        message_id: The Message-ID. Defaults to a unique value.
        body: The text/plain body, or None for none.
        html: The text/html body, or None for none.
        attachments: (filename, content type, bytes) for each attachment.
        headers: Extra headers.
        **overrides: Unused. Present so call sites can pass dicts of options.

    Returns:
        The message bytes with CRLF line endings.
    """
    del overrides
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Date"] = format_datetime(date or datetime(2026, 2, 14, 9, 12, tzinfo=UTC))
    msg["Message-ID"] = message_id or f"<msg{next(_counter)}@example.test>"
    for name, value in (headers or {}).items():
        msg[name] = value
    if body is not None:
        msg.set_content(body)
    if html is not None:
        if body is not None:
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content(html, subtype="html")
    for filename, content_type, data in attachments or []:
        maintype, subtype = content_type.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))


def make_parsed(**options: Any) -> ParsedMessage:
    """Build a ParsedMessage by parsing `make_message(**options)`."""
    raw = make_message(**options)
    header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    fetched = FetchedHeader(
        uid=1,
        flags=frozenset(),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        size=len(raw),
        header=header,
        structure=None,
    )
    return parse_message(fetched, None, whole=raw)
