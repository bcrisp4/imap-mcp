"""Turn a user search into a safe FTS5 query (design T6).

The user's text never reaches `MATCH` as raw syntax. Each word and each quoted
phrase becomes one FTS5 string, so operators such as `NEAR`, `OR`, `*`, `^` or
a column filter in the input are plain keywords.
"""

from __future__ import annotations

import re
from typing import Final

_TOKEN: Final = re.compile(r'"([^"]*)"|(\S+)')
# Characters that carry meaning in FTS5 syntax or that the unicode61
# tokenizer drops anyway.
_STRIP: Final = re.compile(r"[\"(){}\[\]*^:+\-~<>,;!?']")


def build_match(text: str) -> str | None:
    """Build an FTS5 MATCH expression from free text.

    Words are joined with an implicit AND. Text in double quotes is a phrase.

    Args:
        text: The user's search text. Untrusted.

    Returns:
        The MATCH expression, or None if the text holds no searchable word.
    """
    terms: list[str] = []
    for phrase, word in _TOKEN.findall(text):
        raw = phrase if phrase else word
        cleaned = " ".join(_STRIP.sub(" ", raw).split())
        if cleaned:
            terms.append('"' + cleaned.replace('"', '""') + '"')
    return " ".join(terms) if terms else None
