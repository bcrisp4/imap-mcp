"""Progress displays for `imap-mcp sync`.

A terminal gets a live view. Other output, such as a log file, gets structlog
events. Neither shows message content. Folder names are not mail content.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, Self

import structlog
from rich.console import Group
from rich.live import Live
from rich.padding import Padding
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    from rich.console import Console, RenderableType
    from rich.progress import Task

log = structlog.get_logger(__name__)

LOG_INTERVAL_SECONDS: Final = 30.0
_MAX_NAME_WIDTH: Final = 24


def duration(seconds: float) -> str:
    """Format seconds as `1h 05m`, `7m 13s` or `40s`."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class _RateColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        speed = task.speed
        return Text(f"  {speed:,.1f} msg/s" if speed else "", style="dim")


class _TimeLeftColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        left = task.time_remaining
        if left is None:
            return Text("")
        # Rounded to minutes: per-batch speed varies too much for seconds.
        rough = duration(left if left < 60 else round(left / 60) * 60)
        return Text(f"  ~{rough.removesuffix(' 00s')} left", style="cyan")


class TerminalProgress:
    """A live view: one line per finished folder and a bar for the active one.

    The view clears when the sync ends, so only the caller's summary stays.
    """

    def __init__(self, console: Console, account: str) -> None:
        """Create the view.

        Args:
            console: The console to draw on. It must be a terminal.
            account: The IMAP user name, shown in the header.
        """
        self._account = account
        self._started = time.monotonic()
        self._names: list[str] = []
        self._finished: list[tuple[str, int]] = []
        self._active: str | None = None
        self._width = 0
        self._task: TaskID | None = None
        self._bar = Progress(
            SpinnerColumn(),
            TextColumn("{task.description} "),
            BarColumn(bar_width=30),
            TextColumn("{task.completed:>7,.0f} / {task.total:,.0f}"),
            TaskProgressColumn(),
            _RateColumn(),
            _TimeLeftColumn(),
            console=console,
            # A batch of 50 messages can take 20 seconds on iCloud. A long
            # window keeps the rate and time left steady.
            speed_estimate_period=120,
        )
        self._live = Live(
            console=console,
            transient=True,
            get_renderable=self._render,
            refresh_per_second=4,
        )

    def __enter__(self) -> Self:
        self._live.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._live.stop()

    def folders_listed(self, names: Sequence[str]) -> None:
        self._names = list(names)
        self._width = min(max((len(n) for n in names), default=0), _MAX_NAME_WIDTH)

    def folder_started(self, name: str) -> None:
        self._active = name

    def folder_counted(self, name: str, new: int) -> None:
        if new == 0:
            self._finish(name, 0)
            return
        self._task = self._bar.add_task(self._label(name), total=new)

    def batch_synced(self, name: str, done: int, total: int) -> None:
        if self._task is not None:
            self._bar.update(self._task, completed=done)
        if done == total:
            self._finish(name, total)

    def _finish(self, name: str, new: int) -> None:
        if self._task is not None:
            self._bar.remove_task(self._task)
            self._task = None
        self._finished.append((name, new))
        self._active = None

    def _label(self, name: str) -> str:
        return name[: self._width].ljust(self._width)

    def _render(self) -> RenderableType:
        header = Table.grid(expand=True)
        header.add_column()
        header.add_column(justify="right")
        header.add_row(
            Text.assemble(("imap-mcp sync", "bold"), "  ", (self._account, "dim")),
            Text(f"elapsed {duration(time.monotonic() - self._started)}", "dim"),
        )
        rows: list[RenderableType] = [header, Text("")]
        for name, new in list(self._finished):
            line = Text("  ✓ ", style="green")
            line.append(self._label(name), style="bold")
            line.append("  ")
            line.append(f"{new:,} new" if new else "no new mail", style="dim")
            rows.append(line)
        active = self._active
        if active is not None and self._task is None:
            text = Text(self._label(active), style="bold")
            text.append("  checking", style="dim")
            rows.append(Padding(Spinner("dots", text, style="green"), (0, 0, 0, 2)))
        if self._task is not None:
            rows.append(Padding(self._bar, (0, 0, 0, 2)))
        left = len(self._names) - len(self._finished) - (active is not None)
        if left > 0:
            rows.append(Text(f"    {left} folders to check", style="dim"))
        return Group(*rows)


class LogProgress:
    """Writes structlog events, for output that is not a terminal."""

    def __init__(self) -> None:
        """Create the logger state."""
        self._last = 0.0

    def folders_listed(self, names: Sequence[str]) -> None:
        log.info("sync_folders_listed", count=len(names))

    def folder_started(self, name: str) -> None:
        pass

    def folder_counted(self, name: str, new: int) -> None:
        log.info("sync_folder_counted", folder=name, new=new)
        self._last = time.monotonic()

    def batch_synced(self, name: str, done: int, total: int) -> None:
        now = time.monotonic()
        if done == total or now - self._last >= LOG_INTERVAL_SECONDS:
            self._last = now
            log.info("sync_progress", folder=name, done=done, total=total)
