"""Tests for the IMAP command allowlist."""

from __future__ import annotations

import pytest

from imap_mcp.adapters.imap.guard import check_command, check_raw_command
from imap_mcp.domain.errors import GuardViolation

DRAFTS = '"Drafts"'


class TestAllowedCommands:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("CAPABILITY", ()),
            ("LOGIN", ("user", "pass")),
            ("LIST", ('""', "*")),
            ("STATUS", ('"INBOX"', "(MESSAGES UIDNEXT)")),
            ("EXAMINE", ('"INBOX"',)),
            ("NOOP", ()),
            ("LOGOUT", ()),
            ("ID", ("NIL",)),
            ("UID", ("SEARCH", "ALL")),
            ("UID", ("FETCH", "1:5", "(UID FLAGS BODY.PEEK[HEADER] RFC822.SIZE)")),
            ("UID", ("FETCH", "7", "(BODY.PEEK[1.2]<0.1048576>)")),
            ("UID", ("FETCH", "7", "(FLAGS)", "(CHANGEDSINCE 42)")),
            ("APPEND", (DRAFTS, "(\\Draft)", None)),
        ],
        ids=[
            "capability", "login", "list", "status", "examine", "noop", "logout",
            "id", "uid-search", "uid-fetch-header", "uid-fetch-partial",
            "uid-fetch-changedsince", "append-draft",
        ],
    )  # fmt: skip
    def test_the_command_passes(self, name: str, args: tuple[object, ...]) -> None:
        check_command(name, args, DRAFTS)


class TestRefusedCommands:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("SELECT", ('"INBOX"',)),
            ("CLOSE", ()),
            ("EXPUNGE", ()),
            ("STORE", ("1", "+FLAGS", "(\\Seen)")),
            ("COPY", ("1", '"Archive"')),
            ("MOVE", ("1", '"Archive"')),
            ("CREATE", ('"New"',)),
            ("DELETE", ('"Old"',)),
            ("RENAME", ('"A"', '"B"')),
            ("UID", ("STORE", "1", "+FLAGS", "(\\Deleted)")),
            ("UID", ("COPY", "1", '"Archive"')),
            ("UID", ("MOVE", "1", '"Archive"')),
            ("UID", ("EXPUNGE", "1")),
        ],
        ids=[
            "select", "close", "expunge", "store", "copy", "move", "create",
            "delete", "rename", "uid-store", "uid-copy", "uid-move", "uid-expunge",
        ],
    )  # fmt: skip
    def test_a_write_command_raises(self, name: str, args: tuple[object, ...]) -> None:
        with pytest.raises(GuardViolation):
            check_command(name, args, DRAFTS)

    @pytest.mark.parametrize(
        "items",
        ["(BODY[])", "(UID BODY[TEXT])", "(RFC822)", "(RFC822.TEXT)", "(BINARY[1])"],
        ids=["body", "body-text", "rfc822", "rfc822-text", "binary"],
    )
    def test_a_fetch_that_sets_seen_raises(self, items: str) -> None:
        with pytest.raises(GuardViolation, match="PEEK"):
            check_command("UID", ("FETCH", "1", items), DRAFTS)

    def test_rfc822_size_and_header_do_not_set_seen(self) -> None:
        check_command("UID", ("FETCH", "1", "(RFC822.SIZE RFC822.HEADER)"), DRAFTS)

    def test_append_to_another_folder_raises(self) -> None:
        with pytest.raises(GuardViolation, match="Drafts"):
            check_command("APPEND", ('"INBOX"', "(\\Draft)", None), DRAFTS)

    def test_append_without_the_draft_flag_raises(self) -> None:
        with pytest.raises(GuardViolation, match="Draft"):
            check_command("APPEND", (DRAFTS, "(\\Seen)", None), DRAFTS)

    def test_append_raises_when_no_drafts_folder_is_known(self) -> None:
        with pytest.raises(GuardViolation):
            check_command("APPEND", (DRAFTS, "(\\Draft)", None), "")


class TestRawCommands:
    def test_uid_search_passes(self) -> None:
        check_raw_command(b"SEARCH", uid=True)

    @pytest.mark.parametrize(
        ("command", "uid"),
        [(b"SEARCH", False), (b"SORT", True), (b"APPEND", False), (b"ENABLE", False)],
        ids=["search-without-uid", "sort", "multiappend", "enable"],
    )
    def test_other_raw_commands_raise(self, command: bytes, uid: bool) -> None:
        with pytest.raises(GuardViolation):
            check_raw_command(command, uid=uid)
