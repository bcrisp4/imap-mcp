"""The ports and adapters dependency rule, and the no-send rule."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "imap_mcp"
INNER_FORBIDDEN = (
    "imap_mcp.adapters", "imap_mcp.entrypoints", "imap_mcp.bootstrap",
    "imapclient", "sqlite3", "mcp",
)  # fmt: skip


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def modules(*parts: str) -> list[Path]:
    return sorted(PACKAGE.joinpath(*parts).rglob("*.py"))


@pytest.mark.parametrize(
    "path",
    [*modules("domain"), *modules("core"), PACKAGE / "ports.py"],
    ids=lambda p: str(p.relative_to(PACKAGE)),
)
def test_inner_layers_import_no_adapter_or_driver(path: Path) -> None:
    bad = {
        name
        for name in imports(path)
        if any(name == f or name.startswith(f + ".") for f in INNER_FORBIDDEN)
    }

    assert bad == set()


def test_no_module_can_send_mail() -> None:
    senders = {
        str(path.relative_to(PACKAGE))
        for path in modules()
        if {"smtplib", "email.mime.smtp"} & imports(path)
    }

    assert senders == set()


def test_only_the_imap_adapter_imports_imapclient() -> None:
    users = {
        str(path.relative_to(PACKAGE))
        for path in modules()
        if any(n == "imapclient" or n.startswith("imapclient.") for n in imports(path))
    }

    assert users <= {"adapters/imap/session.py", "adapters/imap/structure.py"}
