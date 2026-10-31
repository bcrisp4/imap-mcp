"""A small IMAP server for tests, with TLS and the iCloud quirks on demand.

It is not a conformant server. It implements the commands imap-mcp sends, and
it records every command line so a test can fail on any command outside the
allowlist. A correct server such as Dovecot accepts `SELECT` and `BODY[`
without complaint, so it cannot catch the accidents this project guards
against.

`socketserver` requires handler subclasses. That inheritance is the only kind
in this package.
"""

from __future__ import annotations

import email
import re
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from typing import TYPE_CHECKING, Any, Final, cast

from imap_mcp.adapters.imap.guard import (
    ALLOWED_COMMANDS,
    ALLOWED_UID_COMMANDS,
    DRAFT_FLAG,
    check_fetch_items,
)
from imap_mcp.domain.errors import GuardViolation

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from email.message import Message

USERNAME: Final = "user@example.com"
PASSWORD: Final = "app-password"

# The default 0.5 s shutdown poll adds half a second to every test.
_POLL_INTERVAL: Final = 0.01
_LITERAL: Final = re.compile(rb"\{(\d+)(\+?)\}\r?\n$")
_PLACEHOLDER: Final = re.compile(r"\x00L(\d+)\x00")


@dataclass(frozen=True, slots=True)
class Quirks:
    """iCloud behaviours that a test can switch on. See design Appendix A.

    Attributes:
        no_special_use: LIST reports no SPECIAL-USE attribute, as iCloud does.
        open_ended_fetch_empty: A UID FETCH whose set holds `*` returns no data.
        unsolicited_flags_seq: Send an untagged FETCH with FLAGS and no UID for
            this sequence number before each FETCH answer.
        drop_after_fetches: Close the connection after this many UID FETCH
            commands.
        condstore: Advertise CONDSTORE and report mod-sequences.
    """

    no_special_use: bool = True
    open_ended_fetch_empty: bool = True
    unsolicited_flags_seq: int | None = None
    drop_after_fetches: int | None = None
    condstore: bool = True


@dataclass(slots=True)
class StoredMessage:
    """One message in a fake folder."""

    uid: int
    raw: bytes
    flags: set[str]
    internal_date: datetime
    modseq: int


@dataclass(slots=True)
class Mailbox:
    """One fake folder."""

    name: str
    uidvalidity: int
    special_use: str | None = None
    uidnext: int = 1
    messages: list[StoredMessage] = field(default_factory=list[StoredMessage])


@dataclass(frozen=True, slots=True)
class Recorded:
    """One command line the server received.

    Attributes:
        connection: A number for each connection, in order.
        folder: The folder open when the command arrived.
        line: The command line, with each literal shown as `{n}`.
        tokens: The parsed tokens after the tag.
    """

    connection: int
    folder: str | None
    line: str
    tokens: tuple[Any, ...]


class FakeImapServer:
    """Holds the mailbox state and serves it over TLS on 127.0.0.1."""

    def __init__(
        self, ssl_context: ssl.SSLContext, quirks: Quirks | None = None
    ) -> None:
        """Create the server. Call `start` to listen.

        Args:
            ssl_context: The server TLS context.
            quirks: The quirks to reproduce.
        """
        self.quirks = quirks or Quirks()
        self.mailboxes: dict[str, Mailbox] = {}
        self.commands: list[Recorded] = []
        self.fetch_count = 0
        self.modseq = 1
        self._lock = threading.Lock()
        self._connections = 0
        self.ssl_context = ssl_context
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    # -- state set-up ---------------------------------------------------------

    def add_mailbox(
        self, name: str, *, uidvalidity: int = 1, special_use: str | None = None
    ) -> Mailbox:
        """Create an empty folder."""
        box = Mailbox(name=name, uidvalidity=uidvalidity, special_use=special_use)
        self.mailboxes[name] = box
        return box

    def add_message(
        self,
        folder: str,
        raw: bytes,
        *,
        flags: Iterable[str] = (),
        internal_date: datetime | None = None,
    ) -> int:
        """Store a message and return its UID."""
        with self._lock:
            box = self.mailboxes[folder]
            self.modseq += 1
            uid = box.uidnext
            box.uidnext += 1
            box.messages.append(
                StoredMessage(
                    uid=uid,
                    raw=raw,
                    flags=set(flags),
                    internal_date=internal_date or datetime(2026, 1, 1, tzinfo=UTC),
                    modseq=self.modseq,
                )
            )
            return uid

    def remove_message(self, folder: str, uid: int) -> None:
        """Delete a message, as another client would."""
        with self._lock:
            box = self.mailboxes[folder]
            box.messages = [m for m in box.messages if m.uid != uid]

    def set_flags(self, folder: str, uid: int, flags: Iterable[str]) -> None:
        """Replace the flags of a message, as another client would."""
        with self._lock:
            self.modseq += 1
            for message in self.mailboxes[folder].messages:
                if message.uid == uid:
                    message.flags = set(flags)
                    message.modseq = self.modseq

    def renumber(self, folder: str, uidvalidity: int) -> None:
        """Change UIDVALIDITY and give every message a new UID."""
        with self._lock:
            box = self.mailboxes[folder]
            box.uidvalidity = uidvalidity
            for message in box.messages:
                message.uid = box.uidnext
                box.uidnext += 1

    # -- lifecycle ------------------------------------------------------------

    @property
    def port(self) -> int:
        """The listen port."""
        assert self._server is not None
        return cast("tuple[str, int]", self._server.server_address)[1]

    def start(self) -> None:
        """Listen on an ephemeral port in a daemon thread."""
        fake = self

        class Handler(_Handler):
            server_state = fake

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL},
            daemon=True,
        )  # fmt: skip
        self._thread.start()

    def stop(self) -> None:
        """Stop listening."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def next_connection(self) -> int:
        """Return a number for a new connection."""
        with self._lock:
            self._connections += 1
            return self._connections

    def record(self, entry: Recorded) -> None:
        """Store one received command."""
        with self._lock:
            self.commands.append(entry)

    # -- assertions -----------------------------------------------------------

    def command_verbs(self) -> list[str]:
        """Return each command as `VERB` or `UID VERB`."""
        verbs: list[str] = []
        for entry in self.commands:
            verb = str(entry.tokens[0]).upper() if entry.tokens else ""
            if verb == "UID" and len(entry.tokens) > 1:
                verb = f"UID {str(entry.tokens[1]).upper()}"
            verbs.append(verb)
        return verbs

    def assert_only_allowed(self, drafts_folder: str = "Drafts") -> None:
        """Fail if any received command is outside the production allowlist.

        Args:
            drafts_folder: The only folder APPEND can target.
        """
        for entry in self.commands:
            tokens = entry.tokens
            verb = str(tokens[0]).upper()
            if verb == "UID":
                sub = str(tokens[1]).upper()
                assert sub in ALLOWED_UID_COMMANDS, entry.line
                if sub == "FETCH":
                    try:
                        check_fetch_items(_render(tokens[3:]))
                    except GuardViolation as exc:  # pragma: no cover - failure path
                        raise AssertionError(entry.line) from exc
                continue
            assert verb in ALLOWED_COMMANDS, entry.line
            if verb == "APPEND":
                assert _as_str(tokens[1]) == drafts_folder, entry.line
                assert DRAFT_FLAG in _render(tokens[2]).upper(), entry.line


def _as_str(token: Any) -> str:
    if isinstance(token, bytes):
        return token.decode("utf-8", "replace")
    return str(token)


def _render(token: Any) -> str:
    if isinstance(token, list | tuple):
        return "(" + " ".join(_render(t) for t in cast("Sequence[Any]", token)) + ")"
    return _as_str(token)


# -- parsing ------------------------------------------------------------------


def tokenize(text: str, literals: Sequence[bytes]) -> list[Any]:
    """Split a command into atoms, quoted strings, literals and lists.

    An atom keeps any `[...]` section and `<...>` partial range with it, so
    `BODY.PEEK[HEADER.FIELDS (A B)]<0.10>` is one token.

    Args:
        text: The command text, with each literal replaced by a placeholder.
        literals: The literal bytes, in order.

    Returns:
        The tokens. A list token holds nested tokens.
    """
    stack: list[list[Any]] = [[]]
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == " ":
            i += 1
        elif ch == "(":
            stack.append([])
            i += 1
        elif ch == ")":
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif ch == '"':
            i += 1
            out: list[str] = []
            while text[i] != '"':
                if text[i] == "\\":
                    i += 1
                out.append(text[i])
                i += 1
            stack[-1].append("".join(out))
            i += 1
        elif ch == "\x00":
            match = _PLACEHOLDER.match(text, i)
            assert match is not None
            stack[-1].append(literals[int(match.group(1))])
            i = match.end()
        else:
            start = i
            depth = 0
            while i < len(text):
                c = text[i]
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                elif depth == 0 and c in " ()":
                    break
                i += 1
            stack[-1].append(text[start:i])
    return stack[0]


def parse_uid_set(spec: str, max_uid: int) -> tuple[set[int], bool]:
    """Expand a UID set such as `1:5,9,12:*`.

    Returns:
        The UIDs, and True if the set used `*`.
    """
    uids: set[int] = set()
    star = False
    for piece in spec.split(","):
        if ":" in piece:
            a, b = piece.split(":")
            star = star or "*" in (a, b)
            lo = max_uid if a == "*" else int(a)
            hi = max_uid if b == "*" else int(b)
            lo, hi = min(lo, hi), max(lo, hi)
            uids.update(range(lo, hi + 1))
        elif piece == "*":
            star = True
            uids.add(max_uid)
        else:
            uids.add(int(piece))
    return uids, star


# -- MIME helpers ---------------------------------------------------------------


def _quote(value: str | None) -> str:
    if value is None:
        return "NIL"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _payload_bytes(part: Message) -> bytes:
    payload = cast("str", part.get_payload(decode=False))
    return payload.encode("utf-8", "surrogateescape")


def _params(part: Message) -> str:
    params = [(k, v) for k, v in part.get_params() or [] if v][1:]
    if not params:
        return "NIL"
    return "(" + " ".join(f"{_quote(k.upper())} {_quote(v)}" for k, v in params) + ")"


def body_structure(part: Message) -> str:
    """Render the BODYSTRUCTURE of a message (RFC 3501 section 7.4.2)."""
    if part.is_multipart():
        children = "".join(
            body_structure(p) for p in cast("list[Message]", part.get_payload())
        )
        subtype = _quote(part.get_content_subtype().upper())
        return f"({children} {subtype} {_params(part)} NIL NIL NIL)"
    maintype = part.get_content_maintype().upper()
    subtype = part.get_content_subtype().upper()
    encoding = (part.get("Content-Transfer-Encoding") or "7bit").upper()
    body = _payload_bytes(part)
    fields = (
        f"{_quote(maintype)} {_quote(subtype)} {_params(part)} NIL NIL "
        f"{_quote(encoding)} {len(body)}"
    )
    if maintype == "TEXT":
        fields += f" {body.count(b'\n')}"
    disposition = part.get_content_disposition()
    if disposition:
        filename = part.get_filename()
        disp_params = (
            f"({_quote('FILENAME')} {_quote(filename)})" if filename else "NIL"
        )
        fields += f" NIL ({_quote(disposition.upper())} {disp_params}) NIL NIL"
    return f"({fields})"


def section_part(message: Message, number: str) -> Message:
    """Return the MIME part with an IMAP part number such as `1.2`."""
    part = message
    for index in number.split("."):
        if part.is_multipart():
            part = cast("list[Message]", part.get_payload())[int(index) - 1]
        elif index != "1":
            raise KeyError(number)
    return part


def _header_contains(raw: bytes, name: str, value: str) -> bool:
    parsed = email.message_from_bytes(raw, policy=policy.compat32)
    return value.lower() in str(parsed.get(name, "")).lower()


def header_block(raw: bytes) -> bytes:
    """Return the header block, including the blank line after it."""
    ends = [(raw.find(sep), sep) for sep in (b"\r\n\r\n", b"\n\n")]
    found = [(pos, sep) for pos, sep in ends if pos >= 0]
    if not found:
        return raw
    pos, sep = min(found)
    return raw[: pos + len(sep)]


# -- the protocol handler ---------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    server_state: FakeImapServer

    def setup(self) -> None:
        state = self.server_state
        # A client that rejects the certificate ends the handshake. That is the
        # expected outcome of the TLS test, not a server error.
        self.tls_failed = False
        try:
            self.request = state.ssl_context.wrap_socket(self.request, server_side=True)
        except ssl.SSLError, OSError:
            self.tls_failed = True
        super().setup()
        self.connection_id = state.next_connection()
        self.folder: str | None = None
        self.condstore_enabled = False

    def send(self, text: str | bytes) -> None:
        data = text if isinstance(text, bytes) else text.encode()
        self.wfile.write(data + b"\r\n")

    def read_command(self) -> tuple[str, list[bytes]] | None:
        line = self.rfile.readline()
        if not line:
            return None
        text_parts: list[str] = []
        literals: list[bytes] = []
        while True:
            match = _LITERAL.search(line)
            if not match:
                text_parts.append(line.rstrip(b"\r\n").decode("utf-8", "replace"))
                break
            text_parts.append(line[: match.start()].decode("utf-8", "replace"))
            if not match.group(2):
                self.send("+ Ready")
            literals.append(self.rfile.read(int(match.group(1))))
            text_parts.append(f"\x00L{len(literals) - 1}\x00")
            line = self.rfile.readline()
        return "".join(text_parts), literals

    def handle(self) -> None:
        state = self.server_state
        if self.tls_failed:
            return
        self.send("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] fake ready")
        while True:
            command = self.read_command()
            if command is None:
                return
            text, literals = command
            tokens = tokenize(text, literals)
            tag, rest = str(tokens[0]), tokens[1:]
            sizes = [len(lit) for lit in literals]
            shown = _PLACEHOLDER.sub(
                lambda m, sizes=sizes: "{" + str(sizes[int(m.group(1))]) + "}", text
            )
            state.record(Recorded(self.connection_id, self.folder, shown, tuple(rest)))
            if not self.dispatch(tag, rest):
                return

    def dispatch(self, tag: str, tokens: list[Any]) -> bool:
        verb = str(tokens[0]).upper() if tokens else ""
        args = tokens[1:]
        handlers = {
            "CAPABILITY": self.cmd_capability,
            "LOGIN": self.cmd_login,
            "ID": self.cmd_id,
            "LIST": self.cmd_list,
            "STATUS": self.cmd_status,
            "EXAMINE": self.cmd_examine,
            "SELECT": self.cmd_examine,
            "NOOP": self.cmd_noop,
            "APPEND": self.cmd_append,
            "UID": self.cmd_uid,
        }
        if verb == "LOGOUT":
            self.send("* BYE logging out")
            self.send(f"{tag} OK LOGOUT completed")
            return False
        handler = handlers.get(verb)
        if handler is None:
            self.send(f"{tag} BAD unsupported command")
            return True
        return handler(tag, args)

    def capability_line(self) -> str:
        caps = ["IMAP4rev1", "ID", "UIDPLUS"]
        if self.server_state.quirks.condstore:
            caps.append("CONDSTORE")
        return " ".join(caps)

    def cmd_capability(self, tag: str, _args: list[Any]) -> bool:
        self.send(f"* CAPABILITY {self.capability_line()}")
        self.send(f"{tag} OK CAPABILITY completed")
        return True

    def cmd_login(self, tag: str, args: list[Any]) -> bool:
        if [_as_str(a) for a in args] == [USERNAME, PASSWORD]:
            self.send(f"{tag} OK [CAPABILITY {self.capability_line()}] logged in")
        else:
            self.send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
        return True

    def cmd_id(self, tag: str, _args: list[Any]) -> bool:
        self.send('* ID ("name" "fake")')
        self.send(f"{tag} OK ID completed")
        return True

    def cmd_noop(self, tag: str, _args: list[Any]) -> bool:
        self.send(f"{tag} OK NOOP completed")
        return True

    def cmd_list(self, tag: str, _args: list[Any]) -> bool:
        state = self.server_state
        for box in state.mailboxes.values():
            attrs = ["\\HasNoChildren"]
            if box.special_use and not state.quirks.no_special_use:
                attrs.append(box.special_use)
            self.send(f'* LIST ({" ".join(attrs)}) "/" {_quote(box.name)}')
        self.send(f"{tag} OK LIST completed")
        return True

    def box_status(self, box: Mailbox) -> dict[str, int]:
        unseen = sum(1 for m in box.messages if "\\Seen" not in m.flags)
        highest = max((m.modseq for m in box.messages), default=1)
        return {
            "MESSAGES": len(box.messages), "UIDNEXT": box.uidnext,
            "UIDVALIDITY": box.uidvalidity, "UNSEEN": unseen, "HIGHESTMODSEQ": highest,
        }  # fmt: skip

    def cmd_status(self, tag: str, args: list[Any]) -> bool:
        box = self.server_state.mailboxes.get(_as_str(args[0]))
        if box is None:
            self.send(f"{tag} NO no such folder")
            return True
        values = self.box_status(box)
        wanted = [str(item).upper() for item in cast("list[Any]", args[1])]
        if "HIGHESTMODSEQ" in wanted and not self.server_state.quirks.condstore:
            self.send(f"{tag} BAD HIGHESTMODSEQ needs CONDSTORE")
            return True
        pairs = " ".join(f"{item} {values[item]}" for item in wanted if item in values)
        self.send(f"* STATUS {_quote(box.name)} ({pairs})")
        self.send(f"{tag} OK STATUS completed")
        return True

    def cmd_examine(self, tag: str, args: list[Any]) -> bool:
        box = self.server_state.mailboxes.get(_as_str(args[0]))
        if box is None:
            self.send(f"{tag} NO no such folder")
            return True
        self.folder = box.name
        values = self.box_status(box)
        self.send(f"* {values['MESSAGES']} EXISTS")
        self.send("* 0 RECENT")
        self.send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.send(f"* OK [UIDVALIDITY {box.uidvalidity}] UIDs valid")
        self.send(f"* OK [UIDNEXT {box.uidnext}] next UID")
        if self.server_state.quirks.condstore:
            self.send(f"* OK [HIGHESTMODSEQ {values['HIGHESTMODSEQ']}] modseq")
        self.send(f"{tag} OK [READ-ONLY] EXAMINE completed")
        return True

    def cmd_append(self, tag: str, args: list[Any]) -> bool:
        box = self.server_state.mailboxes.get(_as_str(args[0]))
        if box is None:
            self.send(f"{tag} NO [TRYCREATE] no such folder")
            return True
        flags = (
            [str(f) for f in cast("list[Any]", args[1])]
            if isinstance(args[1], list)
            else []
        )
        raw = cast("bytes", args[-1])
        uid = self.server_state.add_message(box.name, raw, flags=flags)
        self.send(f"{tag} OK [APPENDUID {box.uidvalidity} {uid}] APPEND completed")
        return True

    def cmd_uid(self, tag: str, args: list[Any]) -> bool:
        sub = str(args[0]).upper()
        if self.folder is None:
            self.send(f"{tag} BAD no folder open")
            return True
        box = self.server_state.mailboxes[self.folder]
        if sub == "SEARCH":
            return self.uid_search(tag, box, args[1:])
        if sub == "FETCH":
            return self.uid_fetch(tag, box, args[1:])
        self.send(f"{tag} BAD unsupported UID command")
        return True

    def uid_search(self, tag: str, box: Mailbox, criteria: list[Any]) -> bool:
        matches = list(box.messages)
        i = 0
        while i < len(criteria):
            key = str(criteria[i]).upper()
            if key == "ALL":
                i += 1
            elif key == "HEADER":
                name, value = _as_str(criteria[i + 1]), _as_str(criteria[i + 2])
                matches = [m for m in matches if _header_contains(m.raw, name, value)]
                i += 3
            else:
                self.send(f"{tag} BAD unsupported search key")
                return True
        self.send("* SEARCH " + " ".join(str(m.uid) for m in matches))
        self.send(f"{tag} OK SEARCH completed")
        return True

    def uid_fetch(self, tag: str, box: Mailbox, args: list[Any]) -> bool:
        state = self.server_state
        quirks = state.quirks
        state.fetch_count += 1
        if (
            quirks.drop_after_fetches is not None
            and state.fetch_count > quirks.drop_after_fetches
        ):
            return False
        max_uid = max((m.uid for m in box.messages), default=0)
        uids, star = parse_uid_set(str(args[0]), max_uid)
        items_token = args[1]
        items = (
            [str(t) for t in cast("list[Any]", items_token)]
            if isinstance(items_token, list)
            else [str(items_token)]
        )
        changed_since: int | None = None
        if len(args) > 2:
            modifier = [str(t) for t in cast("list[Any]", args[2])]
            if modifier and modifier[0].upper() == "CHANGEDSINCE":
                changed_since = int(modifier[1])
        if quirks.unsolicited_flags_seq is not None:
            self.send(f"* {quirks.unsolicited_flags_seq} FETCH (FLAGS (\\Seen))")
        if not (star and quirks.open_ended_fetch_empty):
            for seq, message in enumerate(box.messages, start=1):
                if message.uid not in uids:
                    continue
                if changed_since is not None and message.modseq <= changed_since:
                    continue
                self.send_fetch(
                    seq, message, items, include_modseq=changed_since is not None
                )
        self.send(f"{tag} OK FETCH completed")
        return True

    def send_fetch(
        self,
        seq: int,
        message: StoredMessage,
        items: Sequence[str],
        *,
        include_modseq: bool,
    ) -> None:
        parsed = email.message_from_bytes(message.raw, policy=policy.compat32)
        pieces: list[bytes] = [f"UID {message.uid}".encode()]
        literals: list[tuple[str, bytes]] = []
        for item in items:
            upper = item.upper()
            if upper == "UID":
                continue
            if upper == "FLAGS":
                pieces.append(f"FLAGS ({' '.join(sorted(message.flags))})".encode())
            elif upper == "INTERNALDATE":
                stamp = message.internal_date.strftime("%d-%b-%Y %H:%M:%S %z")
                pieces.append(f'INTERNALDATE "{stamp}"'.encode())
            elif upper == "RFC822.SIZE":
                pieces.append(f"RFC822.SIZE {len(message.raw)}".encode())
            elif upper == "MODSEQ":
                pieces.append(f"MODSEQ ({message.modseq})".encode())
            elif upper == "BODYSTRUCTURE":
                pieces.append(f"BODYSTRUCTURE {body_structure(parsed)}".encode())
            elif upper.startswith("BODY.PEEK["):
                key, data = self.section(message, parsed, item)
                literals.append((key, data))
            else:
                pieces.append(f"{upper} NIL".encode())
        if include_modseq and not any(i.upper() == "MODSEQ" for i in items):
            pieces.append(f"MODSEQ ({message.modseq})".encode())
        head = b" ".join(pieces)
        out = f"* {seq} FETCH (".encode() + head
        for key, data in literals:
            out += f" {key} {{{len(data)}}}\r\n".encode() + data
        self.wfile.write(out + b")\r\n")

    def section(
        self, message: StoredMessage, parsed: Message, item: str
    ) -> tuple[str, bytes]:
        match = re.match(
            r"BODY\.PEEK\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", item, re.IGNORECASE
        )
        assert match is not None, item
        spec = match.group(1).upper()
        if spec == "":
            data = message.raw
        elif spec == "HEADER":
            data = header_block(message.raw)
        elif spec.startswith("HEADER.FIELDS"):
            names = {
                n.lower() for n in re.findall(r"[\w-]+", spec[len("HEADER.FIELDS") :])
            }
            lines = [
                f"{k}: {v}\r\n".encode()
                for k, v in parsed.items()
                if k.lower() in names
            ]
            data = b"".join(lines) + b"\r\n"
        else:
            data = _payload_bytes(section_part(parsed, spec))
        key = f"BODY[{match.group(1)}]"
        if match.group(2) is not None:
            origin, length = int(match.group(2)), int(match.group(3))
            data = data[origin : origin + length]
            key += f"<{origin}>"
        return key, data


def mailboxes_from(server: FakeImapServer, spec: Mapping[str, Sequence[bytes]]) -> None:
    """Fill the server from a mapping of folder name to raw messages."""
    for name, raws in spec.items():
        if name not in server.mailboxes:
            server.add_mailbox(name)
        for raw in raws:
            server.add_message(name, raw)
