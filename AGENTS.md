# AGENTS.md

imap-mcp is a local, read-only MCP server for one IMAP mailbox, with a SQLite keyword index. The design doc is the source of truth: [docs/designs/imap-mcp.md](docs/designs/imap-mcp.md).

## Layout (ports and adapters)

| Path | Holds | May import |
|---|---|---|
| `src/imap_mcp/domain/` | Data types and pure rules (MIME parsing, sanitising) | Nothing from the package except `domain` |
| `src/imap_mcp/ports.py` | Protocols for every boundary | `domain` |
| `src/imap_mcp/core/` | Services (sync, read, drafts, attachments) | `domain`, `ports` |
| `src/imap_mcp/adapters/` | IMAP, SQLite, Keychain, lock, file system | Inward layers |
| `src/imap_mcp/entrypoints/` | MCP server and CLI | Inward layers |
| `src/imap_mcp/bootstrap.py` | The only place that builds concrete adapters | Everything |

`tests/unit/test_architecture.py` fails if `domain` or `core` imports an adapter, `imapclient`, `sqlite3` or the MCP SDK, because the core must stay testable without I/O.

## Before you change X, read Y

| Change | Read first |
|---|---|
| Any design decision | "Closed issues" in the design doc. Do not reopen a closed issue without new evidence. |
| `adapters/imap/` | Security T1 and Appendix A (iCloud quirks) |
| `adapters/sqlite/` | Appendix B (SQLite) |
| `core/sync.py` or `adapters/flock.py` | "Concurrency" and Appendix C (sync algorithm) |
| `entrypoints/mcp/views.py` or a tool result | Interfaces and Security T7 |
| Logging | "Logging" in the design doc |

## Rules

- Send no IMAP command outside the allowlist in `adapters/imap/guard.py`. `APPEND` goes to the Drafts folder only. One wrong command can delete or alter real mail, and no undo exists.
- Add no SMTP code and no send feature. An agent that reads hostile mail must have no way to send.
- Put no sender-controlled text in a trusted result field. It goes under `untrusted`, because the agent treats trusted fields as server facts.
- Make no network call inside a database transaction. A stalled IMAP read would hold the write lock and block every other agent process.
- Never log a subject, body, address, snippet, attachment name, search query or password. Logs must be safe to paste into a public issue.
- Never connect to the real mailbox from a test. Use `tests/imapserver/`, which records commands and fails a test on any command outside the allowlist.
- Add no dependency with a GPL-family licence. The project is MIT, and CI runs `scripts/check_licenses.py`.

## After you change X, run Z

| Change | Run |
|---|---|
| Any Python code | `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run mypy`, `uv run pytest` |
| A docstring | `uv run pdoc imap_mcp -o docs/api --docformat google` |
| A dependency | `uv run --no-dev python scripts/check_licenses.py` |
| The design doc | Update its "Updated" date. Move a decided open issue to "Closed issues". |

## Worktrees

Put task worktrees in `.agents/worktrees/<task>/`. Git ignores that directory.
