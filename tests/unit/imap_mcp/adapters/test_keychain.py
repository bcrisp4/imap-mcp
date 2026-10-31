"""Tests for the macOS Keychain credential source."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from imap_mcp.adapters.keychain import SECURITY, SERVICE, KeychainCredentialSource
from imap_mcp.domain.errors import CredentialError
from imap_mcp.ports import CredentialSource


@dataclass
class FakeSecurity:
    """Records `security` invocations and returns canned results."""

    returncode: int = 0
    stdout: str = "app-password\n"
    calls: list[tuple[list[str], bool]] = field(
        default_factory=list[tuple[list[str], bool]]
    )

    def __call__(self, args: list[str], *, capture: bool) -> tuple[int, str]:
        self.calls.append((args, capture))
        return self.returncode, self.stdout if capture else ""


class TestKeychainCredentialSource:
    def test_it_satisfies_the_port(self) -> None:
        source = KeychainCredentialSource("u", FakeSecurity())

        assert isinstance(source, CredentialSource)

    def test_the_password_comes_from_the_imap_mcp_service(self) -> None:
        runner = FakeSecurity()
        source = KeychainCredentialSource("ben@icloud.com", runner)

        assert source.password() == "app-password"
        assert runner.calls == [
            (
                [
                    SECURITY,
                    "find-generic-password",
                    "-s",
                    SERVICE,
                    "-a",
                    "ben@icloud.com",
                    "-w",
                ],
                True,
            )
        ]

    def test_the_keychain_is_read_once_per_process(self) -> None:
        runner = FakeSecurity()
        source = KeychainCredentialSource("ben@icloud.com", runner)

        source.password()
        source.password()

        assert len(runner.calls) == 1

    def test_a_missing_item_tells_the_user_to_run_set_password(self) -> None:
        source = KeychainCredentialSource("u", FakeSecurity(returncode=44, stdout=""))

        with pytest.raises(CredentialError, match="imap-mcp set-password"):
            source.password()

    def test_set_password_lets_security_prompt_so_argv_holds_no_secret(self) -> None:
        runner = FakeSecurity()

        KeychainCredentialSource("ben@icloud.com", runner).store_password()

        args, capture = runner.calls[0]
        assert args[-1] == "-w"
        assert capture is False
        assert args[:2] == [SECURITY, "add-generic-password"]
        assert "-U" in args
