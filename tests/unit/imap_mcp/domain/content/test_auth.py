"""Tests for reading sender authentication verdicts."""

from __future__ import annotations

import pytest

from imap_mcp.domain.content.auth import parse_auth_results
from imap_mcp.domain.enums import AuthVerdict
from imap_mcp.domain.model import AuthResults

PASS, FAIL, NONE, UNKNOWN = (
    AuthVerdict.PASSED,
    AuthVerdict.FAIL,
    AuthVerdict.NONE,
    AuthVerdict.UNKNOWN,
)


class TestSenderTextInsideTheTrustedHeader:
    @pytest.mark.parametrize(
        "header",
        [
            (
                "mx.icloud.com; spf=fail"
                " smtp.mailfrom=dkim=pass.dmarc=pass@evil.example;"
                " dkim=none; dmarc=fail"
            ),
            (
                "mx.icloud.com; spf=fail (domain of dkim=pass dmarc=pass@evil.example"
                " does not designate 192.0.2.1); dkim=none; dmarc=fail"
            ),
            (
                'mx.icloud.com; spf=fail smtp.mailfrom="x;dkim=pass;dmarc=pass"'
                "@evil.example; dkim=none; dmarc=fail"
            ),
            (
                'mx.icloud.com; spf=fail (domain of "a); dkim=pass; dmarc=pass ("'
                "@evil.example does not designate); dkim=none; dmarc=fail"
            ),
        ],
        ids=["property-value", "comment", "quoted-local-part", "quote-in-comment"],
    )
    def test_cannot_add_a_pass_verdict(self, header: str) -> None:
        assert parse_auth_results([header]) == AuthResults(
            spf=FAIL, dkim=NONE, dmarc=FAIL
        )


class TestAuthservId:
    def test_a_top_header_from_another_server_is_ignored(self) -> None:
        forged = "evil.example; spf=pass; dkim=pass; dmarc=pass"

        assert parse_auth_results([forged]) == AuthResults()

    def test_verdicts_come_from_the_trusted_headers_at_the_top(self) -> None:
        headers = [
            "bimi.icloud.com; bimi=skipped",
            "dmarc.icloud.com; dmarc=fail header.from=bank.example",
            "dkim-verifier.icloud.com; dkim=none",
            "spf.icloud.com; spf=softfail smtp.mailfrom=bank.example",
        ]

        assert parse_auth_results(headers) == AuthResults(
            spf=AuthVerdict.SOFTFAIL, dkim=NONE, dmarc=FAIL
        )

    def test_a_lower_header_cannot_override_a_method_already_reported(self) -> None:
        headers = [
            "dmarc.icloud.com; dmarc=fail",
            "dmarc.icloud.com; dmarc=pass",
        ]

        assert parse_auth_results(headers).dmarc is FAIL

    def test_headers_below_an_untrusted_header_are_ignored(self) -> None:
        headers = [
            "spf.icloud.com; spf=fail",
            "relay.example; dkim=pass",
            "dmarc.icloud.com; dmarc=pass",
        ]

        assert parse_auth_results(headers) == AuthResults(spf=FAIL)

    def test_the_trusted_servers_can_be_configured(self) -> None:
        header = "mx.fastmail.com; spf=pass; dkim=pass; dmarc=pass"

        assert parse_auth_results([header], trusted=("fastmail.com",)) == AuthResults(
            spf=PASS, dkim=PASS, dmarc=PASS
        )

    def test_a_lookalike_domain_is_not_trusted(self) -> None:
        header = "evilicloud.com; dmarc=pass"

        assert parse_auth_results([header]).dmarc is UNKNOWN


def test_dkim_passes_when_any_signature_in_the_header_passed() -> None:
    header = "mx.icloud.com; dkim=fail header.d=a.example; dkim=pass header.d=b.example"

    assert parse_auth_results([header]).dkim is PASS
