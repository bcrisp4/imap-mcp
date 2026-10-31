"""UID set rendering.

Every set is closed. iCloud returns nothing for an open-ended range such as
`31:*`, so `*` never appears.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_COMMAND_CHARS: Final = 8192
"""The cap on one rendered UID set.

A set of 40,000 scattered UIDs is about 230 KB on one line, which a server can
refuse.
"""


def render_uid_sets(
    uids: Iterable[int], max_chars: int = MAX_COMMAND_CHARS
) -> list[str]:
    """Render UIDs as closed ranges, split so each part fits `max_chars`.

    Args:
        uids: The UIDs, in any order, with duplicates allowed.
        max_chars: The longest part to return.

    Returns:
        Parts such as `"1:5,9,12:14"`. Empty if `uids` is empty.
    """
    ordered = sorted(set(uids))
    ranges: list[str] = []
    start = prev = None
    for uid in ordered:
        if prev is not None and uid == prev + 1:
            prev = uid
            continue
        if start is not None:
            ranges.append(_range(start, prev))
        start = prev = uid
    if start is not None:
        ranges.append(_range(start, prev))

    parts: list[str] = []
    current: list[str] = []
    length = 0
    for item in ranges:
        extra = len(item) + (1 if current else 0)
        if current and length + extra > max_chars:
            parts.append(",".join(current))
            current, length = [], 0
            extra = len(item)
        current.append(item)
        length += extra
    if current:
        parts.append(",".join(current))
    return parts


def _range(start: int, end: int | None) -> str:
    return str(start) if end is None or end == start else f"{start}:{end}"
