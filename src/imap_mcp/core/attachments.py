"""Attachment download (design S5 and T3).

The sync never downloads attachments. This service fetches one MIME part on
request, from a location whose UIDVALIDITY still matches the index, and hands
the bytes to the file sink under a sanitised name.
"""

from __future__ import annotations

import base64
import binascii
import quopri
import re
import unicodedata
from typing import TYPE_CHECKING, Final

from imap_mcp.domain.content.sanitize import strip_invisible
from imap_mcp.domain.errors import MailError, NotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from imap_mcp.ports import FileSink, Index, MailSourceFactory

MAX_FILENAME_CHARS: Final = 200
_MAX_EXTENSION_CHARS: Final = 10
_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f/\\:]")
_SPACES: Final = re.compile(r"\s+")


def safe_filename(name: str | None, part: str) -> str:
    """Turn a sender-supplied attachment name into a safe base name.

    The result has no path separator, no control or invisible character, no
    leading dot (so it is never hidden or a relative path), and at most 200
    characters with the extension kept.

    Args:
        name: The name the sender gave, or None.
        part: The MIME part number, for a fallback name.

    Returns:
        A file name with no directory part.
    """
    raw = unicodedata.normalize("NFC", strip_invisible(name or ""))
    base = re.split(r"[/\\]", raw)[-1]
    cleaned = _SPACES.sub(" ", _UNSAFE.sub("_", base)).strip().lstrip(". ").strip()
    if not cleaned:
        cleaned = f"attachment-{part.replace('.', '-')}"
    if len(cleaned) > MAX_FILENAME_CHARS:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= _MAX_EXTENSION_CHARS:
            cleaned = stem[: MAX_FILENAME_CHARS - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:MAX_FILENAME_CHARS]
    return cleaned


def decode_part(data: bytes, encoding: str) -> bytes:
    """Undo a content transfer encoding.

    Args:
        data: The encoded part.
        encoding: The transfer encoding, for example "base64".

    Returns:
        The decoded bytes.

    Raises:
        MailError: If base64 data is invalid.
    """
    if encoding == "base64":
        try:
            return base64.b64decode(b"".join(data.split()), validate=False)
        except (binascii.Error, ValueError) as exc:
            raise MailError("The attachment is not valid base64.") from exc
    if encoding == "quoted-printable":
        return quopri.decodestring(data)
    return data


class AttachmentService:
    """Saves one attachment to the download directory."""

    def __init__(
        self, *, index: Index, sources: MailSourceFactory, sink: FileSink
    ) -> None:
        """Create the service.

        Args:
            index: The local index.
            sources: Opens IMAP sessions.
            sink: Writes the file.
        """
        self._index = index
        self._sources = sources
        self._sink = sink

    def download(self, message_id: int, part: str) -> tuple[Path, int]:
        """Fetch one attachment and save it.

        Args:
            message_id: The local message ID.
            part: The MIME part number from the message's attachment list.

        Returns:
            The saved path and the decoded size in bytes.

        Raises:
            NotFoundError: If the message or the part is unknown.
            MailError: If no current location of the message holds it.
        """
        message = self._index.get_message(message_id)
        if message is None:
            msg = f"No message with id {message_id}."
            raise NotFoundError(msg)
        attachment = next(
            (a for a in message.parsed.attachments if a.part == part), None
        )
        if attachment is None:
            msg = f"Message {message_id} has no attachment with part {part}."
            raise NotFoundError(msg)
        data = self._fetch(message_id, part)
        decoded = decode_part(data, attachment.encoding)
        path = self._sink.save(safe_filename(attachment.filename, part), decoded)
        return path, len(decoded)

    def _fetch(self, message_id: int, part: str) -> bytes:
        locations = self._index.locations(message_id)
        with self._sources.open() as source:
            for folder, uid in locations:
                # A UID means nothing outside its UIDVALIDITY. If the folder
                # was renumbered, the UID can name another message.
                if source.examine(folder) != self._index.folder_uidvalidity(folder):
                    continue
                return source.fetch_part(uid, part)
        msg = "The message moved on the server. Run sync_now, then search again."
        raise MailError(msg)
