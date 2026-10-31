# imap-mcp

imap-mcp is a local MCP server that gives an AI agent read-only, searchable access to one IMAP mailbox. It can write drafts and can never send mail.

## Status

Design phase. There is no code yet, so the server does not run. Read the design in [docs/designs/imap-mcp.md](docs/designs/imap-mcp.md).

## What it does

- Syncs one IMAP mailbox to a local SQLite index, and searches it with ranked keyword search.
- Gives an agent tools to search mail, read a message, list folders and save an attachment.
- Writes drafts to the Drafts folder. You review each draft and send it yourself.
- Syncs on a tool call when the data is more than 15 minutes old. Every result tells the agent how old the data is.

## What it never does

- It never sends mail. It contains no SMTP code.
- It never deletes, moves or flags mail, and a read never marks a message as read.
- It never listens on the network. It runs on stdio, started by your agent.

iCloud is the target server. The design uses only standard IMAP, so other servers that accept an app password also work. v1 runs on macOS only, because it stores the password in the macOS Keychain.

## Licence

MIT.
