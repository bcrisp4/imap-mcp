"""Fail if an installed runtime dependency has a GPL-family licence.

imap-mcp is MIT, so a GPL dependency would change the licence of the whole
distribution. Run with `uv run --no-dev python scripts/check_licenses.py`.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import distributions

# AGPL and LGPL also match. LGPL is allowed in principle, but no dependency
# needs it today, so it fails here and gets a human decision.
_COPYLEFT = re.compile(r"\b[AL]?GPL\b|GNU (Affero |Lesser )?General Public", re.I)


def main() -> int:
    """Print each offending distribution and return 1 if any exist."""
    bad: list[str] = []
    for dist in distributions():
        meta = dist.metadata
        fields = [meta.get("License-Expression") or "", meta.get("License") or ""]
        classifiers = meta.get_all("Classifier") or []
        fields += [c for c in classifiers if c.startswith("License")]
        text = " ".join(fields)
        if _COPYLEFT.search(text):
            bad.append(f"{meta['Name']} {meta['Version']}: {text[:120]}")
    for line in bad:
        sys.stderr.write(line + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
