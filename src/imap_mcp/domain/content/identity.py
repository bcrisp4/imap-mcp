"""The identity key that joins copies of one message across folders.

A move on the server is a delete in one folder and a new UID in another. The
sync matches the new UID to stored content by this key, so a moved message is
linked again instead of downloaded again.
"""

from __future__ import annotations

import hashlib


def message_identity(
    *, message_id: str | None, date: str, sender: str, subject: str, size: int
) -> str:
    """Return the identity of a message.

    `Message-ID` alone is not enough: some senders reuse one ID for different
    messages. So the key also covers the raw `Date` and `From` headers. A
    message without a `Message-ID` also uses its subject and size.

    Args:
        message_id: The `Message-ID` header, or None.
        date: The raw `Date` header.
        sender: The raw `From` header.
        subject: The raw `Subject` header.
        size: The RFC822 size.

    Returns:
        A hex SHA-256 digest.
    """
    if message_id and message_id.strip():
        parts = ["mid", message_id.strip().lower(), date.strip(), sender.strip()]
    else:
        parts = ["nomid", date.strip(), sender.strip(), subject.strip(), str(size)]
    return hashlib.sha256(
        "\x00".join(parts).encode("utf-8", "surrogatepass")
    ).hexdigest()
