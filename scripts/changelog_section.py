"""Print the CHANGELOG.md section of one version, for the GitHub release notes.

Usage: `python scripts/changelog_section.py 0.1.0`. It exits 1 if the section
is missing or empty, so a release cannot go out without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)


def section(text: str, version: str) -> str | None:
    """Return the body of the `## [version] - date` section, or None.

    Args:
        text: The changelog text.
        version: The version, without a leading "v".

    Returns:
        The section body without its heading, or None if the section is
        missing, has no release date, or is empty.
    """
    lines = text.splitlines()
    body: list[str] = []
    found = False
    for line in lines:
        match = _HEADING.match(line)
        if match:
            if found:
                break
            if match["version"] == version and match["date"]:
                found = True
            continue
        if found:
            if line.startswith("[") and "]: " in line:
                break
            body.append(line)
    result = "\n".join(body).strip()
    return result or None


def main() -> int:
    """Print the section for `sys.argv[1]`."""
    version = sys.argv[1].removeprefix("v")
    text = Path("CHANGELOG.md").read_text()
    body = section(text, version)
    if body is None:
        sys.stderr.write(
            f"CHANGELOG.md has no dated, non-empty section for {version}.\n"
        )
        return 1
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
