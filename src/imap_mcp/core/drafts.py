"""Draft creation (design S4, T2 and C7).

A draft goes only to the Drafts folder, with the `\\Draft` flag. The body is
Markdown. The HTML part is rendered with raw HTML off and with images turned
into plain links, so opening the draft in a mail client loads nothing from a
remote server.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from typing import TYPE_CHECKING, Any, Final

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

from imap_mcp.domain.errors import InvalidRequestError, NotFoundError
from imap_mcp.domain.model import DraftResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from imap_mcp.ports import Clock, Index, MailSourceFactory

MAX_BODY_BYTES: Final = 1024 * 1024
MAX_RECIPIENTS: Final = 50
MAX_SUBJECT_CHARS: Final = 500


def _image_as_link(
    _renderer: Any, tokens: Sequence[Any], idx: int, _options: Any, _env: Any
) -> str:
    """Render `![alt](url)` as a plain link, so nothing loads on open."""
    token = tokens[idx]
    src = escapeHtml(str(token.attrGet("src") or ""))
    alt = escapeHtml(str(token.content or "")) or src
    return f'<a href="{src}">{alt}</a>'


def render_markdown(body: str) -> str:
    """Render Markdown to HTML with raw HTML off and images as links.

    Args:
        body: The Markdown source.

    Returns:
        The HTML fragment.
    """
    md = MarkdownIt("commonmark", {"html": False})
    md.add_render_rule("image", _image_as_link)
    html: str = md.render(body)
    return html


def _addresses(values: Sequence[str], field: str) -> list[str]:
    out: list[str] = []
    for value in values:
        if "\n" in value or "\r" in value:
            msg = f"{field} contains a line break."
            raise InvalidRequestError(msg)
        _, address = parseaddr(value)
        if "@" not in address or " " in address:
            msg = f"{field} holds an invalid address."
            raise InvalidRequestError(msg)
        out.append(value.strip())
    return out


class DraftService:
    """Writes drafts to the Drafts folder."""

    def __init__(
        self,
        *,
        index: Index,
        sources: MailSourceFactory,
        clock: Clock,
        from_address: str,
    ) -> None:
        """Create the service.

        Args:
            index: The local index.
            sources: Opens IMAP sessions.
            clock: The time source, for the Date header.
            from_address: The From address of every draft.
        """
        self._index = index
        self._sources = sources
        self._clock = clock
        self._from = from_address

    def create(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
        in_reply_to: int | None = None,
    ) -> DraftResult:
        """Compose a draft and store it in the Drafts folder.

        Args:
            to: The To recipients.
            subject: The subject. "Re: " is added for a reply if missing.
            body: The Markdown body.
            cc: The Cc recipients.
            bcc: The Bcc recipients.
            in_reply_to: The local ID of the message this draft answers.

        Returns:
            The new draft's Message-ID, its folder and any new recipients.

        Raises:
            InvalidRequestError: If an input breaks a limit.
            NotFoundError: If `in_reply_to` names an unknown message.
        """
        to_list = _addresses(to, "to")
        cc_list = _addresses(cc, "cc")
        bcc_list = _addresses(bcc, "bcc")
        recipients = [*to_list, *cc_list, *bcc_list]
        if not to_list:
            msg = "A draft needs at least one To recipient."
            raise InvalidRequestError(msg)
        if len(recipients) > MAX_RECIPIENTS:
            msg = f"A draft can have at most {MAX_RECIPIENTS} recipients."
            raise InvalidRequestError(msg)
        if len(body.encode()) > MAX_BODY_BYTES:
            msg = "The draft body is larger than 1 MB."
            raise InvalidRequestError(msg)
        if "\n" in subject or "\r" in subject or len(subject) > MAX_SUBJECT_CHARS:
            msg = "The subject has a line break or is too long."
            raise InvalidRequestError(msg)

        message = EmailMessage()
        message["From"] = self._from
        message["To"] = ", ".join(to_list)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        if bcc_list:
            message["Bcc"] = ", ".join(bcc_list)
        if in_reply_to is not None:
            subject = self._add_reply_headers(message, in_reply_to, subject)
        message["Subject"] = subject
        message["Date"] = format_datetime(self._clock.now())
        domain = self._from.rpartition("@")[2] or None
        message["Message-ID"] = make_msgid(domain=domain)
        message.set_content(body)
        message.add_alternative(render_markdown(body), subtype="html")

        with self._sources.open() as source:
            folder = source.drafts_folder()
            if not folder:
                msg = "No Drafts folder found. Set drafts_folder in the config file."
                raise InvalidRequestError(msg)
            # IMAP and RFC 5322 need CRLF line ends. The default policy uses LF.
            raw = message.as_bytes(policy=message.policy.clone(linesep="\r\n"))
            source.append_draft(folder, raw)

        known = self._index.known_addresses([parseaddr(r)[1] for r in recipients])
        new = tuple(
            parseaddr(r)[1].lower()
            for r in recipients
            if parseaddr(r)[1].lower() not in known
        )
        return DraftResult(
            message_id=str(message["Message-ID"]), folder=folder, new_recipients=new
        )

    def _add_reply_headers(
        self, message: EmailMessage, in_reply_to: int, subject: str
    ) -> str:
        original = self._index.get_message(in_reply_to)
        if original is None:
            msg = f"No message with id {in_reply_to} to reply to."
            raise NotFoundError(msg)
        parent_id = original.parsed.message_id
        if parent_id:
            message["In-Reply-To"] = parent_id
            message["References"] = " ".join((*original.parsed.references, parent_id))
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return subject
