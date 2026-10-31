"""HTML to plain text, with hidden content removed (design T7).

The agent must see what I see in a mail client. Elements that CSS hides are a
common place for injected instructions, so the converter drops them and
reports that it did.

`<style>` rules that hide content by class name are not evaluated. Only inline
styles and the `hidden` attribute are.
"""

from __future__ import annotations

import re
import warnings
from typing import Final

from bs4 import BeautifulSoup, Comment, Tag, XMLParsedAsHTMLWarning

_DROP_TAGS: Final = frozenset(
    {"head", "style", "script", "noscript", "template", "title", "meta", "link"}
)
_BLOCK_TAGS: Final = frozenset(
    {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "ol", "p", "pre", "section", "table", "tr", "ul",
    }
)  # fmt: skip
_LINK_SCHEMES: Final = ("http://", "https://", "mailto:")

_HIDDEN_STYLE: Final = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?:\.0+)?\s*(?:px|pt|em|rem|%)?\s*(?:;|$|!)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$|!)",
    re.IGNORECASE,
)
_ZERO_BOX: Final = re.compile(
    r"(?:max-)?(?:height|width)\s*:\s*0(?:px)?\s*(?:;|$|!)", re.IGNORECASE
)
_OVERFLOW_HIDDEN: Final = re.compile(r"overflow\s*:\s*hidden", re.IGNORECASE)
_TAG_RE: Final = re.compile(r"<[^>]*>")
_SPACES: Final = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES: Final = re.compile(r"\n\s*\n\s*\n+")


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if tag.name == "input" and str(tag.get("type", "")).lower() == "hidden":
        return True
    style = str(tag.get("style", ""))
    if not style:
        return False
    if _HIDDEN_STYLE.search(style):
        return True
    return bool(_ZERO_BOX.search(style) and _OVERFLOW_HIDDEN.search(style))


def _tidy(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, bool]:
    """Convert an HTML body to plain text.

    Links appear as `text (url)` so that a link whose text does not match its
    target is visible. Images appear as their alt text only. Nothing loads.

    Args:
        html: The HTML source. Untrusted.

    Returns:
        The text, and True if the converter removed CSS-hidden elements.
    """
    try:
        return _convert(html)
    except ValueError, TypeError, AttributeError, RecursionError:
        # Markup that the parser cannot handle. Strip tags and keep going: the
        # parser must never stop a sync.
        return _tidy(_TAG_RE.sub(" ", html)), False


def _convert(html: str) -> tuple[str, bool]:
    with warnings.catch_warnings():
        # XHTML mail starts with an XML declaration. A mail client renders it
        # as HTML, so parsing it as HTML is correct, not a mistake to report.
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    hidden_removed = False
    for tag in soup.find_all():
        if not tag.decomposed and _is_hidden(tag):
            tag.decompose()
            hidden_removed = True
    for img in soup.find_all("img"):
        alt = str(img.get("alt", "")).strip()
        img.replace_with(f" {alt} " if alt else " ")
    for link in soup.find_all("a"):
        href = str(link.get("href", "")).strip()
        text = link.get_text(" ", strip=True)
        target = href.removeprefix("mailto:")
        if href.lower().startswith(_LINK_SCHEMES) and target != text:
            link.replace_with(f"{text} ({href})" if text else href)
        else:
            link.replace_with(text)
    for tag in soup.find_all(list(_BLOCK_TAGS)):
        if tag.name == "li":
            tag.insert_before("\n- ")
        else:
            tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" ")
    return _tidy(soup.get_text()), hidden_removed
