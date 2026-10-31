"""MIME parsing: header bytes and text parts in, a `ParsedMessage` out.

Real mail carries broken headers, unknown charsets and impossible dates. The
parser never raises. A message whose text cannot be extracted keeps its
headers and gets `ParseState.FAILED`, so one bad message never stops a sync.
"""

from __future__ import annotations

import base64
import binascii
import quopri
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Final, cast

from imap_mcp.domain.content.auth import parse_auth_results
from imap_mcp.domain.content.html import html_to_text
from imap_mcp.domain.content.identity import message_identity
from imap_mcp.domain.content.sanitize import strip_invisible
from imap_mcp.domain.enums import ParseState
from imap_mcp.domain.model import (
    AttachmentInfo,
    FetchedHeader,
    MessageStructure,
    ParsedMessage,
    TextPart,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from email.message import Message

MAX_TEXT_BYTES: Final = 1024 * 1024
"""The most text fetched from one message (design T6)."""

_EARLIEST: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _decode_words(value: str | None) -> str:
    """Decode RFC 2047 encoded words. Never raises."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except ValueError, LookupError, UnicodeError, TypeError:
        return value


def _header(msg: Message, name: str) -> str:
    raw = msg.get(name)
    return "" if raw is None else str(raw)


def _addresses(msg: Message, name: str) -> tuple[str, ...]:
    values = [_header(msg, name)] if msg.get(name) else []
    try:
        pairs = getaddresses(values)
    except ValueError, TypeError, IndexError:
        return ()
    return tuple(addr.lower() for _, addr in pairs if "@" in addr)


def _date(raw: str, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        parsed = parsedate_to_datetime(raw)
    except ValueError, TypeError, IndexError, OverflowError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        parsed = parsed.astimezone(UTC)
    except ValueError, OverflowError:
        return fallback
    return parsed if parsed >= _EARLIEST else fallback


def _ids(raw: str) -> tuple[str, ...]:
    return tuple(token for token in raw.split() if token.startswith("<"))


def _transfer_decode(data: bytes, encoding: str, *, truncated: bool) -> bytes:
    if encoding == "base64":
        cleaned = b"".join(data.split())
        if truncated:
            cleaned = cleaned[: len(cleaned) - len(cleaned) % 4]
        try:
            return base64.b64decode(cleaned + b"=" * (-len(cleaned) % 4))
        except binascii.Error, ValueError:
            return b""
    if encoding == "quoted-printable":
        return quopri.decodestring(data)
    return data


def _charset_decode(data: bytes, charset: str | None) -> str:
    try:
        return data.decode(charset or "utf-8", "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _part_text(part: TextPart, data: bytes, max_bytes: int) -> tuple[str, bool, bool]:
    """Return (text, truncated, hidden_removed) for one fetched text part."""
    truncated = len(data) >= max_bytes
    raw = _transfer_decode(data, part.encoding, truncated=truncated)
    text = _charset_decode(raw, part.charset)
    if part.subtype == "html":
        text, hidden = html_to_text(text)
        return text, truncated, hidden
    return text, truncated, False


def text_from_parts(
    structure: MessageStructure, texts: Mapping[str, bytes], max_bytes: int
) -> tuple[str, bool, bool]:
    """Join the text parts of a message.

    Args:
        structure: The message structure.
        texts: The fetched bytes of each text part, by part number.
        max_bytes: The fetch cap that applied to each part.

    Returns:
        The text, True if any part hit the cap, and True if hidden HTML was
        removed.
    """
    chunks: list[str] = []
    truncated = hidden = False
    for part in structure.text_parts:
        data = texts.get(part.part)
        if data is None:
            continue
        text, part_truncated, part_hidden = _part_text(part, data, max_bytes)
        chunks.append(text)
        truncated = truncated or part_truncated
        hidden = hidden or part_hidden
    return "\n\n".join(c for c in chunks if c), truncated, hidden


def _walk(msg: Message, number: str = "") -> Iterator[tuple[str, Message]]:
    """Yield (IMAP part number, part) for each leaf part."""
    if msg.is_multipart():
        for index, child in enumerate(cast("list[Message]", msg.get_payload()), 1):
            yield from _walk(child, f"{number}.{index}" if number else str(index))
        return
    yield number or "1", msg


def structure_from_message(msg: Message) -> tuple[MessageStructure, dict[str, bytes]]:
    """Build a structure and the text bytes from a whole message.

    The sync uses this only when BODYSTRUCTURE is missing or cannot be parsed.

    Args:
        msg: The parsed message.

    Returns:
        The structure, and the encoded bytes of each text part.
    """
    texts: list[TextPart] = []
    attachments: list[AttachmentInfo] = []
    data: dict[str, bytes] = {}
    for number, part in _walk(msg):
        content_type = part.get_content_type()
        encoding = _header(part, "Content-Transfer-Encoding").lower() or "7bit"
        payload = part.get_payload(decode=False)
        raw = (
            payload.encode("utf-8", "surrogateescape")
            if isinstance(payload, str)
            else b""
        )
        is_attachment = part.get_content_disposition() == "attachment"
        if content_type in {"text/plain", "text/html"} and not is_attachment:
            texts.append(
                TextPart(
                    part=number,
                    subtype=part.get_content_subtype(),
                    charset=part.get_content_charset(),
                    encoding=encoding,
                    size=len(raw),
                )
            )
            data[number] = raw
        else:
            filename = part.get_filename()
            attachments.append(
                AttachmentInfo(
                    part=number,
                    filename=_decode_words(filename) if filename else None,
                    content_type=content_type,
                    size=len(raw),
                    encoding=encoding,
                )
            )
    html = [t for t in texts if t.subtype == "html"]
    chosen = tuple(html) if html else tuple(texts)
    return MessageStructure(text_parts=chosen, attachments=tuple(attachments)), data


def parse_message(
    fetched: FetchedHeader,
    texts: Mapping[str, bytes] | None,
    *,
    whole: bytes | None = None,
    max_bytes: int = MAX_TEXT_BYTES,
) -> ParsedMessage:
    """Parse one message for the index.

    Args:
        fetched: The header fetch result.
        texts: The fetched text parts, by part number, when the structure was
            known.
        whole: The first `max_bytes` of the whole message, when the structure
            was not known.
        max_bytes: The fetch cap.

    Returns:
        The parsed message. Never raises.
    """
    msg = message_from_bytes(fetched.header, policy=policy.compat32)
    message_id = _header(msg, "Message-ID").strip() or None
    raw_date = _header(msg, "Date")
    raw_from = _header(msg, "From")
    raw_subject = _header(msg, "Subject")
    name, address = parseaddr(_decode_words(raw_from))
    structure = fetched.structure or MessageStructure()
    body = ""
    truncated = hidden = False
    state = ParseState.OK
    try:
        if fetched.structure is None and whole is not None:
            whole_msg = message_from_bytes(whole, policy=policy.compat32)
            structure, whole_texts = structure_from_message(whole_msg)
            body, _, hidden = text_from_parts(structure, whole_texts, max_bytes + 1)
            truncated = len(whole) >= max_bytes
        elif texts is not None:
            body, truncated, hidden = text_from_parts(structure, texts, max_bytes)
    except ValueError, TypeError, LookupError, UnicodeError, IndexError, AttributeError:
        state = ParseState.FAILED
        body = ""
    return ParsedMessage(
        identity=message_identity(
            message_id=message_id,
            date=raw_date,
            sender=raw_from,
            subject=raw_subject,
            size=fetched.size,
        ),
        message_id=message_id,
        date=_date(raw_date, fetched.internal_date),
        from_name=strip_invisible(name),
        from_address=address.lower(),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        subject=strip_invisible(_decode_words(raw_subject)),
        body_text=strip_invisible(body)[:max_bytes],
        text_truncated=truncated,
        hidden_content_removed=hidden,
        auth=parse_auth_results(
            [str(v) for v in msg.get_all("Authentication-Results") or []]
        ),
        in_reply_to=_header(msg, "In-Reply-To").strip() or None,
        references=_ids(_header(msg, "References")),
        parse_state=state,
        size=fetched.size,
        attachments=structure.attachments,
    )
