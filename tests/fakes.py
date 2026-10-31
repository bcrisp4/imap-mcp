"""Fake implementations of the ports, for core tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass
class FakeCredentialSource:
    """Returns a fixed password."""

    value: str = "app-password"

    def password(self) -> str:
        """Return the password."""
        return self.value


@dataclass
class FakeClock:
    """A clock that moves only when a test moves it."""

    current: datetime = field(
        default_factory=lambda: datetime(2026, 10, 31, 21, 0, tzinfo=UTC)
    )
    mono: float = 1000.0
    step: float = 0.0

    def now(self) -> datetime:
        """Return the current fake time."""
        return self.current

    def monotonic(self) -> float:
        """Return the fake monotonic time, then add `step`."""
        value = self.mono
        self.mono += self.step
        return value

    def advance(self, seconds: float) -> None:
        """Move both clocks forward."""
        self.current += timedelta(seconds=seconds)
        self.mono += seconds


@dataclass
class FakeLock:
    """A sync lock that another process can hold."""

    held_elsewhere: bool = False
    held: bool = False
    acquisitions: int = 0

    def try_acquire(self) -> bool:
        """Take the lock if nobody else holds it."""
        if self.held_elsewhere:
            return False
        self.held = True
        self.acquisitions += 1
        return True

    def acquire(self, timeout: float) -> bool:
        """Same as try_acquire. Tests do not wait."""
        del timeout
        return self.try_acquire()

    def release(self) -> None:
        """Release the lock."""
        self.held = False


@dataclass
class FakeFileSink:
    """Keeps saved files in memory."""

    directory: Path = Path("/downloads")
    saved: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def save(self, filename: str, data: bytes) -> Path:
        """Store the file and return its path."""
        self.saved[filename] = data
        return self.directory / filename
