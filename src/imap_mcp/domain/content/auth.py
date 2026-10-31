"""Sender authentication results (RFC 8601).

Only the topmost `Authentication-Results` header counts. The receiving server
adds its header above every header that was already in the message, so a
sender can forge a lower header but not the top one.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from imap_mcp.domain.enums import AuthVerdict
from imap_mcp.domain.model import AuthResults

if TYPE_CHECKING:
    from collections.abc import Sequence

_RESULT: Final = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)
_VERDICTS: Final = {v.value: v for v in AuthVerdict}


def parse_auth_results(headers: Sequence[str]) -> AuthResults:
    """Read SPF, DKIM and DMARC verdicts from the topmost header.

    Args:
        headers: The `Authentication-Results` values, in message order.

    Returns:
        The verdicts. A method that the header does not mention is UNKNOWN.
        DKIM is PASS if any DKIM signature passed.
    """
    if not headers:
        return AuthResults()
    found: dict[str, list[AuthVerdict]] = {"spf": [], "dkim": [], "dmarc": []}
    for method, result in _RESULT.findall(headers[0]):
        found[method.lower()].append(_VERDICTS.get(result.lower(), AuthVerdict.UNKNOWN))

    def pick(method: str) -> AuthVerdict:
        verdicts = found[method]
        if AuthVerdict.PASS in verdicts:
            return AuthVerdict.PASS
        return verdicts[0] if verdicts else AuthVerdict.UNKNOWN

    return AuthResults(spf=pick("spf"), dkim=pick("dkim"), dmarc=pick("dmarc"))
