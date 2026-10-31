"""The macOS Keychain credential source.

It calls the system binary `/usr/bin/security` instead of a Python library.
Keychain access approval belongs to the binary that reads the item, so an
approval for `/usr/bin/security` survives a venv rebuild or a Python upgrade.
"""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING, Final, Protocol

from imap_mcp.domain.errors import CredentialError

if TYPE_CHECKING:
    from collections.abc import Sequence

SECURITY: Final = "/usr/bin/security"
SERVICE: Final = "imap-mcp"
_ITEM_NOT_FOUND: Final = 44


class CommandRunner(Protocol):
    """Runs `security` and returns its exit code and standard output."""

    def __call__(self, args: list[str], *, capture: bool) -> tuple[int, str]:
        """Run a command.

        Args:
            args: The full argv.
            capture: Capture standard output. When False, the command uses the
                terminal, so `security` can prompt for input.

        Returns:
            The exit code and the captured output (empty when not captured).
        """
        ...


def run_command(args: Sequence[str], *, capture: bool) -> tuple[int, str]:
    """Run a command without a shell.

    Args:
        args: The full argv. The first item is an absolute path.
        capture: Capture standard output.

    Returns:
        The exit code and the captured output.
    """
    # S603: argv starts with a fixed absolute path and runs without a shell.
    result = subprocess.run(  # noqa: S603
        list(args), capture_output=capture, text=True, check=False
    )
    return result.returncode, result.stdout if capture else ""


class KeychainCredentialSource:
    """Reads the IMAP app password from the login Keychain.

    The password stays in memory after the first read, so the Keychain is
    read at most once for each agent session.
    """

    def __init__(self, username: str, runner: CommandRunner = run_command) -> None:
        """Create the source.

        Args:
            username: The IMAP username, used as the Keychain account.
            runner: Runs `security`. Tests replace it.
        """
        self._username = username
        self._runner = runner
        self._cached: str | None = None
        self._lock = threading.Lock()

    def password(self) -> str:
        """Return the password.

        Returns:
            The password.

        Raises:
            CredentialError: If the Keychain has no item or refuses access.
        """
        with self._lock:
            if self._cached is None:
                self._cached = self._read()
            return self._cached

    def store_password(self) -> None:
        """Create or replace the Keychain item.

        `-w` is the last argument and has no value, so `security` prompts on
        the terminal and the password never appears in a process list.

        Raises:
            CredentialError: If `security` fails.
        """
        args = [
            SECURITY, "add-generic-password", "-U",
            "-s", SERVICE, "-a", self._username, "-l", SERVICE, "-w",
        ]  # fmt: skip
        code, _ = self._runner(args, capture=False)
        if code != 0:
            msg = f"security add-generic-password failed with exit code {code}."
            raise CredentialError(msg)

    def _read(self) -> str:
        args = [
            SECURITY, "find-generic-password",
            "-s", SERVICE, "-a", self._username, "-w",
        ]  # fmt: skip
        code, out = self._runner(args, capture=True)
        if code == _ITEM_NOT_FOUND:
            msg = "No password in the Keychain. Run `imap-mcp set-password`."
            raise CredentialError(msg)
        if code != 0:
            msg = f"The Keychain refused access (security exit code {code})."
            raise CredentialError(msg)
        password = out.removesuffix("\n")
        if not password:
            msg = "The Keychain item is empty. Run `imap-mcp set-password`."
            raise CredentialError(msg)
        return password
