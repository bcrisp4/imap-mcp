"""Sender authentication results (RFC 8601).

Only headers that the receiving server added count. The receiving server adds
its headers above every header that was already in the message, so the trusted
headers are the run at the top whose authserv-id names a trusted server. The
first header in that run that reports a method gives its verdict. A sender can
forge a lower header with a trusted authserv-id, but not one above the
receiver's own header for the same method.

Inside a header, only the `method=result` token at the start of each
`;`-separated clause counts. Comments and property values such as
`smtp.mailfrom` hold sender-controlled text, so they never give a verdict.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from imap_mcp.domain.enums import AuthVerdict
from imap_mcp.domain.model import AuthResults

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_AUTH_SERVERS: Final = ("icloud.com",)
_METHODS: Final = ("spf", "dkim", "dmarc")
_RESULT: Final = re.compile(r"(spf|dkim|dmarc)\s*=\s*([a-z]+)\b", re.IGNORECASE)
_VERDICTS: Final = {v.value: v for v in AuthVerdict}


def parse_auth_results(
    headers: Sequence[str], trusted: Sequence[str] = DEFAULT_AUTH_SERVERS
) -> AuthResults:
    """Read SPF, DKIM and DMARC verdicts from the receiver's headers.

    Args:
        headers: The `Authentication-Results` values, in message order.
        trusted: Domains of the receiving server. An authserv-id matches if it
            equals one of them or is a subdomain of one.

    Returns:
        The verdicts. A method that no trusted header mentions is UNKNOWN.
        DKIM is PASSED if any DKIM signature in its header passed.
    """
    domains = tuple(d.lower().strip(".") for d in trusted)
    found: dict[str, AuthVerdict] = {}
    for header in headers:
        clauses = _clauses(header)
        if not clauses or not _is_trusted(clauses[0], domains):
            break
        here: dict[str, list[AuthVerdict]] = {}
        for clause in clauses[1:]:
            match = _RESULT.match(clause)
            if match:
                verdict = _VERDICTS.get(match[2].lower(), AuthVerdict.UNKNOWN)
                here.setdefault(match[1].lower(), []).append(verdict)
        for method, verdicts in here.items():
            if method not in found:
                passed = AuthVerdict.PASSED in verdicts
                found[method] = AuthVerdict.PASSED if passed else verdicts[0]
    return AuthResults(**{m: found.get(m, AuthVerdict.UNKNOWN) for m in _METHODS})


def _clauses(header: str) -> list[str]:
    """Split a header on `;` outside quoted strings, with comments removed.

    A quoted string is tracked inside a comment too. A server can copy a
    quoted local-part such as `"a); dmarc=pass ("@evil.example` into a
    comment, and its `)` must not end the comment.
    """
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    quoted = escaped = False
    for char in header:
        if escaped:
            escaped = False
            if depth == 0:
                current.append(char)
        elif char == "\\" and (quoted or depth):
            escaped = True
        elif quoted:
            if depth == 0:
                current.append(char)
            quoted = char != '"'
        elif char == '"':
            quoted = True
            if depth == 0:
                current.append(char)
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif depth:
            continue
        elif char == ";":
            clauses.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    clauses.append("".join(current).strip())
    return clauses


def _is_trusted(authserv: str, domains: Sequence[str]) -> bool:
    # The authserv-id may be followed by a version number.
    parts = authserv.split()
    server = parts[0].lower().strip(".") if parts else ""
    return any(server == d or server.endswith("." + d) for d in domains)
