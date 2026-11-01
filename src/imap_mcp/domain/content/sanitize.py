"""Removal of invisible characters from untrusted text (design T7).

Most reviewers cannot see these characters, but a model reads them. A payload
written in Unicode TAG characters, for example, is invisible in a mail client
and plain text to the model.
"""

from __future__ import annotations

import re
from typing import Final

# TAG block, bidi embeddings and isolates, bidi marks, zero-width space, word
# joiner and invisible operators, zero-width no-break space (BOM), Mongolian
# vowel separator, soft hyphen. ZWJ (U+200D) and ZWNJ (U+200C) stay, because
# emoji sequences and several scripts need them.
_INVISIBLE: Final = re.compile(
    r"[\U000e0000-\U000e007f\u202a-\u202e\u2066-\u2069\u200e\u200f"
    r"\u200b\u2060-\u2064\ufeff\u180e\u00ad]"
)

_CONTENT_TAG: Final = re.compile(r"</?\s*email-content\b[^>]*>?", re.IGNORECASE)


def strip_invisible(text: str) -> str:
    """Remove invisible and direction-control characters.

    Args:
        text: Untrusted text.

    Returns:
        The text without those characters.
    """
    return _INVISIBLE.sub("", text)


def strip_content_tags(text: str) -> str:
    """Remove anything that looks like an `email-content` boundary tag.

    The server wraps untrusted text in these tags. A sender who writes one into
    a message could otherwise close the boundary early.

    Args:
        text: Untrusted text.

    Returns:
        The text with each boundary tag replaced by `_`.
    """
    # Replace each tag with "_", not "". Removing an inner tag could join the
    # text around it into a new tag, and a repeat until stable is quadratic.
    # "_" cannot be part of a tag, so one linear pass is enough.
    return _CONTENT_TAG.sub("_", text)
