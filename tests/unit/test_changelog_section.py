"""Tests for the release-notes extraction script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "changelog_section.py"

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Next thing.

## [0.2.0] - 2026-12-01

### Fixed

- A bug.

## [0.1.0] - 2026-11-01

### Added

- First release.

[unreleased]: https://github.com/bcrisp4/imap-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bcrisp4/imap-mcp/compare/v0.1.0...v0.2.0
"""


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog_section", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSection:
    def test_a_middle_section_stops_at_the_next_heading(self) -> None:
        assert load().section(CHANGELOG, "0.2.0") == "### Fixed\n\n- A bug."

    def test_the_last_section_stops_at_the_link_references(self) -> None:
        assert load().section(CHANGELOG, "0.1.0") == "### Added\n\n- First release."

    def test_unreleased_and_unknown_versions_give_none(self) -> None:
        module = load()

        assert module.section(CHANGELOG, "Unreleased") is None
        assert module.section(CHANGELOG, "9.9.9") is None
