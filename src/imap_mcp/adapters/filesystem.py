"""The download directory. Implements `FileSink`.

Each file is created with exclusive create, so an existing file is never
overwritten, and gets the macOS quarantine attribute so Gatekeeper treats it
as a downloaded file.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from pathlib import Path
from typing import Final

import structlog

from imap_mcp.domain.errors import ImapMcpError

log = structlog.get_logger(__name__)

XATTR: Final = "/usr/bin/xattr"
QUARANTINE: Final = "com.apple.quarantine"
_MAX_SUFFIX: Final = 1000


class DownloadDirectory:
    """Saves files in one directory and never outside it."""

    def __init__(self, directory: Path, *, quarantine: bool = True) -> None:
        """Create the sink.

        Args:
            directory: The download directory.
            quarantine: Set the quarantine attribute. Tests turn it off on
                file systems without extended attributes.
        """
        self._directory = directory
        self._quarantine = quarantine

    def save(self, filename: str, data: bytes) -> Path:
        """Write a new file. Add ` (n)` before the extension if the name exists.

        Args:
            filename: A base name with no directory part.
            data: The file contents.

        Returns:
            The path written.

        Raises:
            ImapMcpError: If the name escapes the directory, no free name
                exists, or the quarantine attribute cannot be set.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        root = self._directory.resolve()
        stem, suffix = Path(filename).stem, Path(filename).suffix
        for n in range(_MAX_SUFFIX):
            name = filename if n == 0 else f"{stem} ({n}){suffix}"
            path = root / name
            if path.resolve().parent != root:
                msg = "The attachment name points outside the download directory."
                raise ImapMcpError(msg)
            try:
                with path.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                continue
            self._mark_quarantined(path)
            return path
        msg = "No free file name in the download directory."
        raise ImapMcpError(msg)

    def _mark_quarantined(self, path: Path) -> None:
        if not self._quarantine:
            return
        value = f"0081;{int(time.time()):x};imap-mcp;"
        # S603: argv starts with a fixed absolute path and runs without a shell.
        result = subprocess.run(  # noqa: S603
            [XATTR, "-w", QUARANTINE, value, str(path)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            # Fail closed: an unquarantined download skips Gatekeeper checks.
            with contextlib.suppress(OSError):
                path.unlink()
            msg = "Could not set the quarantine attribute, so the file was removed."
            raise ImapMcpError(msg)
