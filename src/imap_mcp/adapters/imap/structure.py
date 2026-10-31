"""Turn an imapclient `BodyData` (a parsed BODYSTRUCTURE) into text parts and
attachments.

The sync fetches only the text parts it finds here, so attachment bytes never
cross the network during a sync (design C9).

Field positions follow RFC 3501 section 7.4.2. A single part is
`(type subtype params id description encoding size ...)`. A TEXT part adds a
line count, and a MESSAGE/RFC822 part adds an envelope, a body and a line
count, before the extension fields (md5, disposition, language, location).
"""

# imapclient ships no type information, so its values are untyped here and
# cast at the point of use.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false

from __future__ import annotations

from email.header import decode_header, make_header
from typing import TYPE_CHECKING, Any, Final, cast

from imap_mcp.domain.model import AttachmentInfo, MessageStructure, TextPart

if TYPE_CHECKING:
    from collections.abc import Sequence

_TEXT_SUBTYPES: Final = frozenset({"plain", "html"})
_DISPOSITION_INDEX: Final = {"text": 9, "message/rfc822": 11}
_DEFAULT_DISPOSITION_INDEX: Final = 8


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _pairs(value: Any) -> dict[str, str]:
    if not isinstance(value, tuple | list):
        return {}
    items = cast("Sequence[Any]", value)
    return {_s(items[i]).lower(): _s(items[i + 1]) for i in range(0, len(items) - 1, 2)}


def _decode_filename(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except ValueError, LookupError:
        return raw


def parse_structure(body: Any) -> MessageStructure:
    """Find the text parts to index and the attachments to list.

    In a `multipart/alternative`, the HTML part wins over the plain part,
    because the HTML part is what a mail client shows. A payload hidden only
    in the plain part never reaches the agent, and a payload hidden in the HTML
    is removed by the HTML-to-text step.

    Args:
        body: The `BodyData` from a BODYSTRUCTURE fetch.

    Returns:
        The text parts in display order, and the attachments.
    """
    texts: list[TextPart] = []
    attachments: list[AttachmentInfo] = []
    _walk(body, "", texts, attachments)
    return MessageStructure(text_parts=tuple(texts), attachments=tuple(attachments))


def _walk(
    body: Any,
    number: str,
    texts: list[TextPart],
    attachments: list[AttachmentInfo],
) -> None:
    if isinstance(body[0], list):
        children: list[Any] = body[0]
        subtype = _s(body[1]).lower()
        numbers = [
            f"{number}.{i}" if number else str(i) for i in range(1, len(children) + 1)
        ]
        if subtype == "alternative":
            _walk_alternative(children, numbers, texts, attachments)
            return
        for child, child_number in zip(children, numbers, strict=True):
            _walk(child, child_number, texts, attachments)
        return

    part_number = number or "1"
    maintype = _s(body[0]).lower()
    subtype = _s(body[1]).lower()
    params = _pairs(body[2])
    encoding = _s(body[5]).lower() or "7bit"
    size = int(body[6]) if isinstance(body[6], int) else 0
    full_type = f"{maintype}/{subtype}"
    index = _DISPOSITION_INDEX.get(
        full_type, _DISPOSITION_INDEX.get(maintype, _DEFAULT_DISPOSITION_INDEX)
    )
    disposition = body[index] if len(body) > index else None
    disp_type = ""
    disp_params: dict[str, str] = {}
    if isinstance(disposition, tuple | list) and disposition:
        disp = cast("Sequence[Any]", disposition)
        disp_type = _s(disp[0]).lower()
        disp_params = _pairs(disp[1]) if len(disp) > 1 else {}
    filename_raw = disp_params.get("filename") or params.get("name")
    filename = _decode_filename(filename_raw) if filename_raw else None

    if maintype == "text" and subtype in _TEXT_SUBTYPES and disp_type != "attachment":
        texts.append(
            TextPart(
                part=part_number,
                subtype=subtype,
                charset=params.get("charset"),
                encoding=encoding,
                size=size,
            )
        )
        return
    if maintype == "multipart":
        return
    attachments.append(
        AttachmentInfo(
            part=part_number,
            filename=filename,
            content_type=full_type,
            size=size,
            encoding=encoding,
        )
    )


def _walk_alternative(
    children: list[Any],
    numbers: list[str],
    texts: list[TextPart],
    attachments: list[AttachmentInfo],
) -> None:
    candidates: list[tuple[list[TextPart], list[AttachmentInfo]]] = []
    for child, child_number in zip(children, numbers, strict=True):
        child_texts: list[TextPart] = []
        child_attachments: list[AttachmentInfo] = []
        _walk(child, child_number, child_texts, child_attachments)
        candidates.append((child_texts, child_attachments))
    html = [c for c in candidates if any(t.subtype == "html" for t in c[0])]
    chosen = html[-1] if html else next((c for c in reversed(candidates) if c[0]), None)
    if chosen is None:
        return
    texts.extend(chosen[0])
    attachments.extend(chosen[1])
