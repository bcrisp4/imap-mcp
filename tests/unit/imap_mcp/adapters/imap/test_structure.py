"""Tests for BODYSTRUCTURE parsing, with responses shaped like real servers'."""

from __future__ import annotations

from typing import Any

from imapclient.response_parser import parse_fetch_response

from imap_mcp.adapters.imap.structure import parse_structure


def structure_of(text: bytes) -> Any:
    response = parse_fetch_response([b"1 (UID 1 BODYSTRUCTURE " + text + b")"])
    return response[1][b"BODYSTRUCTURE"]


ALTERNATIVE_WITH_PDF = (
    b'((("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 120 4'
    b" NIL NIL NIL)"
    b'("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 900 20 NIL NIL NIL)'
    b' "ALTERNATIVE" ("BOUNDARY" "b2") NIL NIL)'
    b'("APPLICATION" "PDF" ("NAME" "renewal.pdf") NIL NIL "BASE64" 184220 NIL'
    b' ("ATTACHMENT" ("FILENAME" "renewal.pdf")) NIL)'
    b' "MIXED" ("BOUNDARY" "b1") NIL NIL)'
)

FORWARDED_MESSAGE = (
    b'(("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 20 1 NIL NIL NIL)'
    b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 500'
    b' ("Mon, 1 Jan 2026 00:00:00 +0000" "inner" NIL NIL NIL NIL NIL NIL NIL "<i@x>")'
    b' ("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 10 1 NIL NIL NIL) 12'
    b' NIL ("ATTACHMENT" ("FILENAME" "fwd.eml")) NIL)'
    b' "MIXED" ("BOUNDARY" "b") NIL NIL)'
)


class TestParseStructure:
    def test_html_wins_in_an_alternative_and_the_pdf_is_an_attachment(self) -> None:
        structure = parse_structure(structure_of(ALTERNATIVE_WITH_PDF))

        (text,) = structure.text_parts
        assert (text.part, text.subtype, text.charset, text.encoding) == (
            "1.2", "html", "utf-8", "quoted-printable",
        )  # fmt: skip
        (attachment,) = structure.attachments
        assert (attachment.part, attachment.filename, attachment.size) == (
            "2", "renewal.pdf", 184220,
        )  # fmt: skip

    def test_a_single_part_message_is_part_one(self) -> None:
        body = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 42 2 NIL NIL NIL)'

        (text,) = parse_structure(structure_of(body)).text_parts

        assert text.part == "1"

    def test_a_forwarded_message_is_an_attachment_and_is_not_entered(self) -> None:
        structure = parse_structure(structure_of(FORWARDED_MESSAGE))

        assert [t.part for t in structure.text_parts] == ["1"]
        (attachment,) = structure.attachments
        assert (attachment.part, attachment.content_type, attachment.filename) == (
            "2", "message/rfc822", "fwd.eml",
        )  # fmt: skip

    def test_an_encoded_filename_is_decoded(self) -> None:
        body = (
            b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL'
            b' ("ATTACHMENT" ("FILENAME" "=?utf-8?q?R=C3=A9sum=C3=A9.pdf?=")) NIL)'
        )

        (attachment,) = parse_structure(structure_of(body)).attachments

        assert attachment.filename == "Résumé.pdf"

    def test_a_text_attachment_is_not_taken_as_the_body(self) -> None:
        body = (
            b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 5 1 NIL NIL NIL)'
            b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 900 30 NIL'
            b' ("ATTACHMENT" ("FILENAME" "log.txt")) NIL)'
            b' "MIXED" ("BOUNDARY" "b") NIL NIL)'
        )

        structure = parse_structure(structure_of(body))

        assert [t.part for t in structure.text_parts] == ["1"]
        assert [a.filename for a in structure.attachments] == ["log.txt"]
