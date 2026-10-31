"""The bundle of application services that an entry point drives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from imap_mcp.core.attachments import AttachmentService
    from imap_mcp.core.drafts import DraftService
    from imap_mcp.core.search import ReadService
    from imap_mcp.core.sync import SyncService
    from imap_mcp.ports import Clock, Index


@dataclass(frozen=True, slots=True)
class Services:
    """Every service, built once by `imap_mcp.bootstrap`."""

    index: Index
    clock: Clock
    sync: SyncService
    read: ReadService
    attachments: AttachmentService
    drafts: DraftService
