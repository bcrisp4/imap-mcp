# AGENTS.md

imap-mcp is a local, read-only MCP server for one IMAP mailbox, with a SQLite keyword index. The project is in the design phase. The design doc is the source of truth: [docs/designs/imap-mcp.md](docs/designs/imap-mcp.md).

## Before you change X, read Y

| Change | Read first |
|---|---|
| Any design decision | The "Closed issues" section of the design doc. Do not reopen a closed issue without new evidence. |
| IMAP code (`MailSession`) | Security T1 and Appendix A (iCloud quirks) |
| Schema, queries or connection setup | Appendix B (SQLite) |
| Sync engine or the sync lock | Architecture "Concurrency" and Appendix C (sync algorithm) |
| The shape of a tool result | Interfaces and Security T7 |
| Logging | The "Logging" section |

## Rules

- Send no IMAP command outside the allowlist in Security T1. `APPEND` goes to the Drafts folder only. One wrong command can delete or alter real mail, and no undo exists.
- Add no SMTP code and no send feature. An agent that reads hostile mail must have no way to send.
- Put no email content in a trusted result field. Subject, sender name, snippet, body and attachment names go in `untrusted`, because the agent treats trusted fields as instructions from the server.
- Make no network call inside a database transaction. A stalled IMAP read holds the write lock and blocks every other agent process.
- Never log a subject, body, address, snippet, attachment name, search query or password. Logs must be safe to paste into a public issue.
- Never connect to the real mailbox from a test. Use the fake IMAP server, which records commands and reproduces the iCloud quirks.
- Add no dependency with a GPL-family licence. The project is MIT.

## After you change X, run Z

The commands below apply from milestone M1, when the Python project exists.

| Change | Run |
|---|---|
| Any Python code | `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest` |
| The design doc | Update its "Updated" date. Move a decided open issue to "Closed issues". |

## Worktrees

Put task worktrees in `.agents/worktrees/<task>/`. Git ignores that directory.
