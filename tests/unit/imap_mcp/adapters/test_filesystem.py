"""Tests for the download directory (design T3)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from imap_mcp.adapters.filesystem import QUARANTINE, XATTR, DownloadDirectory
from imap_mcp.core.attachments import safe_filename
from imap_mcp.domain.errors import ImapMcpError
from imap_mcp.ports import FileSink

if TYPE_CHECKING:
    from pathlib import Path


class TestDownloadDirectory:
    def test_it_satisfies_the_port(self, tmp_path: Path) -> None:
        assert isinstance(DownloadDirectory(tmp_path), FileSink)

    def test_a_saved_file_carries_the_quarantine_attribute(
        self, tmp_path: Path
    ) -> None:
        path = DownloadDirectory(tmp_path).save("a.pdf", b"%PDF")

        result = subprocess.run(  # noqa: S603 - fixed absolute binary, no shell
            [XATTR, "-p", QUARANTINE, str(path)], capture_output=True, check=False
        )

        assert result.returncode == 0
        assert result.stdout.endswith(b";imap-mcp;\n")

    def test_an_existing_file_is_never_overwritten(self, tmp_path: Path) -> None:
        (tmp_path / "a.pdf").write_bytes(b"mine")
        sink = DownloadDirectory(tmp_path)

        first = sink.save("a.pdf", b"new")
        second = sink.save("a.pdf", b"newer")

        assert (tmp_path / "a.pdf").read_bytes() == b"mine"
        assert (first.name, second.name) == ("a (1).pdf", "a (2).pdf")

    def test_a_name_that_escapes_the_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImapMcpError):
            DownloadDirectory(tmp_path / "dl").save("../escape.txt", b"x")


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../.zshrc", "zshrc"),
            ("C:\\Windows\\evil.exe", "evil.exe"),
            ("in\x00voice\x07.pdf", "in_voice_.pdf"),
            ("..", "attachment-2"),
            (None, "attachment-2"),
            ("report" + chr(0x202E) + "fdp.exe", "reportfdp.exe"),
            ("  .hidden  ", "hidden"),
        ],
        ids=["traversal", "windows-path", "control-chars", "dots-only", "none",
             "rlo-override", "leading-dot"],
    )  # fmt: skip
    def test_the_name_is_a_safe_base_name(self, raw: str | None, expected: str) -> None:
        assert safe_filename(raw, "2") == expected

    def test_a_long_multibyte_name_fits_the_file_system_limit(self) -> None:
        name = safe_filename("\U0001f4c4" * 200 + ".pdf", "2")

        assert len(name.encode()) <= 255
        assert name.endswith(".pdf")

    def test_a_long_name_keeps_its_extension(self) -> None:
        name = safe_filename("x" * 300 + ".pdf", "2")

        assert len(name) == 200
        assert name.endswith(".pdf")
