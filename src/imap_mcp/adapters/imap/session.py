"""The IMAP adapter: the only module that drives imapclient.

Every transport and protocol failure leaves this module as `MailError`, so the
core catches one type. The command guard from `guard.py` is installed on every
client right after it connects, before login.
"""

# imapclient ships no type information, so its values are untyped here and
# cast at the point of use.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import contextlib
import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

import structlog
from imapclient import IMAPClient, SocketTimeout
from imapclient.exceptions import IMAPClientError
from imapclient.imapclient import seq_to_parenstr_upper
from imapclient.response_parser import parse_fetch_response

from imap_mcp.adapters.imap.guard import GuardHandle, install_guard
from imap_mcp.adapters.imap.structure import parse_structure
from imap_mcp.adapters.imap.uidset import render_uid_sets
from imap_mcp.domain.enums import FolderRole
from imap_mcp.domain.errors import GuardViolation, MailError
from imap_mcp.domain.folders import folder_role
from imap_mcp.domain.model import FetchedHeader, Folder, FolderStatus

if TYPE_CHECKING:
    from collections.abc import Collection, Generator, Sequence

    from imap_mcp.domain.model import TextPart
    from imap_mcp.ports import CredentialSource

log = structlog.get_logger(__name__)

CONNECT_TIMEOUT: Final = 15.0
READ_TIMEOUT: Final = 60.0

_HEADER_ITEMS: Final = (
    "UID", "FLAGS", "INTERNALDATE", "RFC822.SIZE", "BODYSTRUCTURE", "BODY.PEEK[HEADER]",
)  # fmt: skip
_STATUS_ITEMS: Final = ("MESSAGES", "UIDNEXT", "UIDVALIDITY", "UNSEEN")
_TRANSPORT_ERRORS: Final = (IMAPClientError, OSError, EOFError)


def _s(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


@contextlib.contextmanager
def _mail_errors(operation: str) -> Generator[None]:
    """Turn every transport or protocol error into `MailError`."""
    try:
        yield
    except GuardViolation:
        raise
    except _TRANSPORT_ERRORS as exc:
        raise MailError(f"IMAP {operation} failed: {exc}") from exc
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        # A server that leaves out an item the client asked for.
        raise MailError(f"IMAP {operation} returned an unexpected response") from exc


def _find_section(item: dict[Any, Any], prefix: str) -> bytes | None:
    """Find a body section by prefix.

    The server writes the key it echoes, without PEEK and often with the
    partial origin, for example `BODY[1]<0>`. Case and spacing vary, so match
    the prefix only.
    """
    wanted = prefix.upper()
    for key, value in item.items():
        if _s(key).upper().startswith(wanted):
            return cast("bytes | None", value)
    return None


class ImapMailSource:
    """One logged-in IMAP session. Implements `MailSource`."""

    def __init__(self, client: Any, guard: GuardHandle) -> None:
        """Wrap a connected, logged-in client that already has the guard.

        Args:
            client: The `IMAPClient`.
            guard: The handle of the installed guard.
        """
        self._client = client
        self._guard = guard
        self._capabilities: frozenset[str] | None = None

    def capabilities(self) -> frozenset[str]:
        """Return the post-login capabilities, upper case."""
        if self._capabilities is None:
            with _mail_errors("CAPABILITY"):
                raw = self._client.capabilities()
            self._capabilities = frozenset(_s(c).upper() for c in raw)
        return self._capabilities

    def list_folders(self) -> list[Folder]:
        """Return every selectable folder with its role."""
        with _mail_errors("LIST"):
            rows = self._client.list_folders()
        folders: list[Folder] = []
        for flags, _delimiter, name in rows:
            flag_names = [_s(f) for f in flags]
            if any(f.upper() == "\\NOSELECT" for f in flag_names):
                continue
            folders.append(
                Folder(name=_s(name), role=folder_role(_s(name), flag_names))
            )
        return folders

    def status(self, folder: str) -> FolderStatus:
        """Return STATUS values, with HIGHESTMODSEQ when CONDSTORE exists."""
        condstore = "CONDSTORE" in self.capabilities()
        what = [*_STATUS_ITEMS, "HIGHESTMODSEQ"] if condstore else list(_STATUS_ITEMS)
        with _mail_errors("STATUS"):
            raw = self._client.folder_status(folder, what)
            values = {_s(k).upper(): int(v) for k, v in raw.items()}
            return FolderStatus(
                messages=values["MESSAGES"],
                uidnext=values["UIDNEXT"],
                uidvalidity=values["UIDVALIDITY"],
                unseen=values["UNSEEN"],
                highestmodseq=values.get("HIGHESTMODSEQ"),
            )

    def examine(self, folder: str) -> int:
        """Open a folder read-only (EXAMINE) and return its UIDVALIDITY."""
        with _mail_errors("EXAMINE"):
            response = self._client.select_folder(folder, readonly=True)
            return int(response[b"UIDVALIDITY"])

    def search_uids(self) -> list[int]:
        """Return every UID in the open folder."""
        with _mail_errors("UID SEARCH"):
            return sorted(int(u) for u in self._client.search(["ALL"]))

    def search_message_id(self, message_id: str) -> list[int]:
        """Return UIDs in the open folder whose `Message-ID` matches."""
        with _mail_errors("UID SEARCH"):
            found = self._client.search(["HEADER", "Message-ID", message_id])
            return sorted(int(u) for u in found)

    def _fetch(
        self,
        uids: Collection[int],
        items: Sequence[str],
        modifiers: list[str] | None = None,
    ) -> dict[int, dict[Any, Any]]:
        """Run UID FETCH and key the rows on the UID inside each row.

        `IMAPClient.fetch` keys rows on the UID only when a row carries one.
        An unsolicited FETCH (another device marked a message read) carries a
        sequence number and no UID, so imapclient keys it on that number and
        can merge it into the row of an unrelated message with that UID. This
        method parses with sequence-number keys instead, which merges an
        unsolicited row only into the row of the same message, and drops rows
        that carry no UID.
        """
        wanted = set(uids)
        rows: dict[int, dict[Any, Any]] = {}
        imap = self._client._imap
        for part in render_uid_sets(wanted):
            args: list[Any] = ["UID", "FETCH", part, seq_to_parenstr_upper(list(items))]
            if modifiers:
                args.append(seq_to_parenstr_upper(modifiers))
            with _mail_errors("UID FETCH"):
                tag = imap._command(*args)
                typ, data = imap._command_complete("FETCH", tag)
                self._client._checkok("fetch", typ, data)
                typ, data = imap._untagged_response(typ, data, "FETCH")
                response = parse_fetch_response(
                    data, normalise_times=False, uid_is_key=False
                )
            for item in response.values():
                uid = item.get(b"UID")
                if isinstance(uid, int) and uid in wanted:
                    rows[uid] = item
        return rows

    def fetch_headers(self, uids: Collection[int]) -> list[FetchedHeader]:
        """Return flags, dates, sizes, headers and structure for these UIDs.

        A message that comes back without a required item is left out and
        logged. The next sync tries it again.
        """
        headers: list[FetchedHeader] = []
        for uid, item in sorted(self._fetch(uids, _HEADER_ITEMS).items()):
            header = _find_section(item, "BODY[HEADER")
            date = item.get(b"INTERNALDATE")
            size = item.get(b"RFC822.SIZE")
            if header is None or not isinstance(date, datetime) or size is None:
                log.warning("imap_fetch_item_missing", uid=uid)
                continue
            structure = None
            body = item.get(b"BODYSTRUCTURE")
            if body is not None:
                try:
                    structure = parse_structure(body)
                except IndexError, TypeError, ValueError:
                    log.warning("imap_bodystructure_unparsed", uid=uid)
            headers.append(
                FetchedHeader(
                    uid=uid,
                    flags=frozenset(_s(f) for f in item.get(b"FLAGS", ())),
                    internal_date=date if date.tzinfo else date.replace(tzinfo=UTC),
                    size=int(size),
                    header=bytes(header),
                    structure=structure,
                )
            )
        return headers

    def fetch_text(
        self, uid: int, parts: Sequence[TextPart], max_bytes: int
    ) -> dict[str, bytes]:
        """Return each text part's encoded bytes, capped at `max_bytes`."""
        if not parts:
            return {}
        items = [f"BODY.PEEK[{p.part}]<0.{max_bytes}>" for p in parts]
        item = self._fetch([uid], items).get(uid, {})
        out: dict[str, bytes] = {}
        for p in parts:
            data = _find_section(item, f"BODY[{p.part}]")
            if data is not None:
                out[p.part] = bytes(data)
        return out

    def fetch_whole(self, uid: int, max_bytes: int) -> bytes:
        """Return the first `max_bytes` of the whole message."""
        item = self._fetch([uid], [f"BODY.PEEK[]<0.{max_bytes}>"]).get(uid, {})
        data = _find_section(item, "BODY[]")
        if data is None:
            raise MailError("IMAP UID FETCH returned no message body")
        return bytes(data)

    def fetch_flags(
        self, uids: Collection[int], changed_since: int | None = None
    ) -> dict[int, frozenset[str]]:
        """Return flags, optionally only for messages changed since a modseq."""
        modifiers = (
            [f"CHANGEDSINCE {changed_since}"] if changed_since is not None else None
        )
        rows = self._fetch(uids, ["UID", "FLAGS"], modifiers)
        return {
            uid: frozenset(_s(f) for f in item.get(b"FLAGS", ()))
            for uid, item in rows.items()
        }

    def fetch_part(self, uid: int, part: str) -> bytes:
        """Return the encoded bytes of one MIME part."""
        item = self._fetch([uid], [f"BODY.PEEK[{part}]"]).get(uid)
        data = _find_section(item, f"BODY[{part}]") if item else None
        if data is None:
            raise MailError("IMAP UID FETCH returned no data for that part")
        return bytes(data)

    def append_draft(self, folder: str, message: bytes) -> None:
        """Store a message in the Drafts folder with the `\\Draft` flag."""
        with _mail_errors("APPEND"):
            self._client.append(folder, message, flags=[b"\\Draft"])


class ImapMailSourceFactory:
    """Opens guarded IMAP sessions. Implements `MailSourceFactory`."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        credentials: CredentialSource,
        drafts_folder: str = "",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Create the factory.

        Args:
            host: The IMAP host.
            port: The implicit-TLS port, normally 993.
            username: The login name.
            credentials: Supplies the password.
            drafts_folder: The configured Drafts folder. Empty means find it.
            ssl_context: A TLS context. Only tests pass one. The default checks
                the certificate and the host name.
        """
        self._host = host
        self._port = port
        self._username = username
        self._credentials = credentials
        self._drafts_folder = drafts_folder
        self._ssl_context = ssl_context or ssl.create_default_context()

    @contextlib.contextmanager
    def open(self) -> Generator[ImapMailSource]:
        """Connect over TLS, install the guard, log in and find Drafts.

        Yields:
            The session. It logs out when the block exits.

        Raises:
            MailError: If the connection or the login fails.
        """
        password = self._credentials.password()
        with _mail_errors("connect"):
            client = IMAPClient(
                self._host,
                port=self._port,
                ssl=True,
                ssl_context=self._ssl_context,
                # imapclient annotates `timeout` as float, but documents and
                # accepts SocketTimeout for separate connect and read limits.
                timeout=cast(
                    "float",
                    SocketTimeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
                ),
            )
        guard = install_guard(client)
        # The default True returns INTERNALDATE as naive local time.
        client.normalise_times = False
        try:
            with _mail_errors("LOGIN"):
                client.login(self._username, password)
            source = ImapMailSource(client, guard)
            guard.allow_drafts_folder(self._resolve_drafts(source))
            yield source
        finally:
            with contextlib.suppress(*_TRANSPORT_ERRORS):
                # LOGOUT, never CLOSE: CLOSE expunges.
                client.logout()

    def _resolve_drafts(self, source: ImapMailSource) -> str:
        folders = source.list_folders()
        if self._drafts_folder:
            return (
                self._drafts_folder
                if any(f.name == self._drafts_folder for f in folders)
                else ""
            )
        return next((f.name for f in folders if f.role is FolderRole.DRAFTS), "")
