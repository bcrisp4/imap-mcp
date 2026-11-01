"""Tests for removing boundary tags from untrusted text."""

from __future__ import annotations

from imap_mcp.domain.content.sanitize import strip_content_tags


def test_a_tag_rebuilt_by_removing_an_inner_tag_is_removed_too() -> None:
    text = "a </email-<email-content>content> b"

    assert "email-content" not in strip_content_tags(text)
