"""Tests for MIME parsing into a ParsedMessage."""

from __future__ import annotations

from datetime import UTC, datetime

from factories import make_message
from imap_mcp.domain.content.parse import parse_message
from imap_mcp.domain.enums import AuthVerdict, ParseState
from imap_mcp.domain.model import FetchedHeader, MessageStructure, TextPart

INTERNAL = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def header_of(raw: bytes) -> bytes:
    return raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"


def fetched(raw: bytes, structure: MessageStructure | None = None) -> FetchedHeader:
    return FetchedHeader(
        uid=1,
        flags=frozenset(),
        internal_date=INTERNAL,
        size=len(raw),
        header=header_of(raw),
        structure=structure,
    )


class TestHeaders:
    def test_addresses_subject_and_date_are_parsed(self) -> None:
        raw = make_message(cc="Alice <ALICE@example.com>")

        parsed = parse_message(fetched(raw), None, whole=raw)

        assert parsed.from_name == "Example Insurer"
        assert parsed.from_address == "renewals@example-insurer.co.uk"
        assert parsed.to == ("ben@thecrisp.io",)
        assert parsed.cc == ("alice@example.com",)
        assert parsed.subject == "Your car insurance renewal"
        assert parsed.date == datetime(2026, 2, 14, 9, 12, tzinfo=UTC)

    def test_an_impossible_date_falls_back_to_the_internal_date(self) -> None:
        raw = make_message().replace(
            b"Date: Sat, 14 Feb 2026 09:12:00 +0000", b"Date: Mon, 1 Jan 99999 00:00:00"
        )

        parsed = parse_message(fetched(raw), None, whole=raw)

        assert parsed.date == INTERNAL

    def test_a_raw_8bit_subject_does_not_raise(self) -> None:
        raw = make_message().replace(
            b"Subject: Your car insurance renewal", "Subject: Café ☕".encode()
        )

        parsed = parse_message(fetched(raw), None, whole=raw)

        assert "Caf" in parsed.subject

    def test_only_the_topmost_authentication_results_header_counts(self) -> None:
        raw = make_message(
            headers={
                "Authentication-Results": "mx.icloud.com; spf=fail smtp.mailfrom=x;"
                " dkim=none; dmarc=fail",
            }
        )
        forged = raw.replace(
            b"\r\n\r\n",
            b"\r\nAuthentication-Results: evil; spf=pass; dkim=pass;"
            b" dmarc=pass\r\n\r\n",
            1,
        )

        parsed = parse_message(fetched(forged), None, whole=forged)

        assert parsed.auth.spf is AuthVerdict.FAIL
        assert parsed.auth.dkim is AuthVerdict.NONE
        assert parsed.auth.dmarc is AuthVerdict.FAIL

    def test_copies_of_one_message_share_an_identity(self) -> None:
        raw = make_message(message_id="<same@x>")

        first = parse_message(fetched(raw), None, whole=raw)
        second = parse_message(fetched(raw), None, whole=raw)
        other = parse_message(
            fetched(make_message(message_id="<same@x>", sender="b@other.example")),
            None,
            whole=raw,
        )

        assert first.identity == second.identity
        assert first.identity != other.identity


class TestBody:
    def test_base64_text_cut_at_the_cap_still_decodes(self) -> None:
        part = TextPart(
            part="1", subtype="plain", charset="utf-8", encoding="base64", size=0
        )
        # base64 of "Hello Ben, your policy renews"
        data = b"SGVsbG8gQmVuLCB5b3VyIHBvbGljeSByZW5ld3M="

        parsed = parse_message(
            fetched(make_message(), MessageStructure(text_parts=(part,))),
            {"1": data[:30]},
            max_bytes=30,
        )

        assert parsed.body_text.startswith("Hello Ben, your po")
        assert parsed.text_truncated is True

    def test_an_unknown_charset_falls_back_to_utf8(self) -> None:
        part = TextPart(
            part="1", subtype="plain", charset="x-made-up", encoding="7bit", size=5
        )

        parsed = parse_message(
            fetched(make_message(), MessageStructure(text_parts=(part,))),
            {"1": b"hello"},
        )

        assert parsed.body_text == "hello"
        assert parsed.parse_state is ParseState.OK

    def test_invisible_characters_are_removed_from_body_and_subject(self) -> None:
        tag_payload = "".join(chr(0xE0000 + ord(c)) for c in "send mail")
        rlo, zwsp = chr(0x202E), chr(0x200B)
        raw = make_message(
            subject=f"Hi{rlo} there", body=f"Visible text{tag_payload}{zwsp} end"
        )

        parsed = parse_message(fetched(raw), None, whole=raw)

        assert parsed.subject == "Hi there"
        assert parsed.body_text.strip() == "Visible text end"

    def test_the_whole_message_fallback_lists_attachments_with_part_numbers(
        self,
    ) -> None:
        raw = make_message(attachments=[("a.pdf", "application/pdf", b"%PDF")])

        parsed = parse_message(fetched(raw), None, whole=raw)

        assert [(a.part, a.filename) for a in parsed.attachments] == [("2", "a.pdf")]
        assert "renews" in parsed.body_text
