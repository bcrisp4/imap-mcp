# imap-mcp

imap-mcp is a local MCP server that gives an AI agent read-only, searchable access to one IMAP mailbox. It can write drafts and can never send mail.

It syncs the mailbox into a local SQLite index with ranked keyword search (FTS5). It syncs on a tool call when the data is more than 15 minutes old, and every result tells the agent how old the data is. iCloud is the main target. Any IMAP server that accepts an app password also works.

## What it never does

- It never sends mail. The package contains no SMTP code.
- It never deletes, moves or flags mail. A read never marks a message as read. A command allowlist checks every IMAP command before it goes on the wire.
- It never listens on the network. It runs on stdio, started by your agent.

Email is untrusted input. Results keep sender text apart from server data, remove hidden HTML and invisible Unicode, and show sender authentication results. See [Security](docs/designs/imap-mcp.md#security) in the design doc.

## Requirements

- macOS. The password lives in the macOS Keychain.
- [uv](https://docs.astral.sh/uv/).
- An IMAP account with an app password. For iCloud, create one at [account.apple.com](https://account.apple.com) under Sign-In and Security, App-Specific Passwords.

## Quick start

1. Clone the repo:

   ```bash
   git clone https://github.com/bcrisp4/imap-mcp ~/src/imap-mcp
   ```

2. Create `~/.config/imap-mcp/config.toml`:

   ```toml
   host = "imap.mail.me.com"
   port = 993
   username = "you@icloud.com"
   from_address = "you@icloud.com"
   ```

3. Store the app password in the Keychain. When macOS asks, choose Always Allow for `/usr/bin/security`:

   ```bash
   uv run --directory ~/src/imap-mcp imap-mcp set-password
   ```

4. Check the connection. This prints the server capabilities and folders:

   ```bash
   uv run --directory ~/src/imap-mcp imap-mcp probe
   ```

5. Run the first full sync. A large mailbox takes a long time, because iCloud limits download speed. You can stop it at any time and run it again later. It continues where it stopped:

   ```bash
   uv run --directory ~/src/imap-mcp imap-mcp sync
   ```

### Optional settings

| Key | Default | Effect |
|---|---|---|
| `download_dir` | `~/Downloads` | Where `download_attachment` saves files |
| `sync_interval_minutes` | `15` | How old the data can be before a tool call syncs |
| `sync_budget_seconds` | `20` | The time limit of a sync that a tool call starts |
| `db_path` | `~/Library/Application Support/imap-mcp/imap-mcp.db` | The index. It must be on a local disk, not in iCloud Drive. |
| `drafts_folder` | empty | The Drafts folder name. Empty means find it by role or name. |
| `exclude_folders` | `[]` | Folders never to sync |

`IMAP_MCP_CONFIG` overrides the config file path.

## Connect the Claude desktop app

1. Find the absolute path of `uv`. The desktop app starts servers with a minimal `PATH`, so a bare `uv` fails. Do not use a version-manager shim:

   ```bash
   which -a uv
   ```

2. Open Claude, then Settings, Developer, Edit Config. This opens `~/Library/Application Support/Claude/claude_desktop_config.json`.

3. Add the server. Replace the `uv` path and the repo path with yours:

   ```json
   {
     "mcpServers": {
       "imap": {
         "command": "/opt/homebrew/bin/uv",
         "args": ["run", "--directory", "/Users/you/src/imap-mcp", "imap-mcp", "serve"]
       }
     }
   }
   ```

4. Quit and restart Claude. The tools appear under the imap server in the tools menu.

5. Keep `create_draft` on "Ask" when Claude asks for permission. Do not choose "Always allow".

Server logs go to `~/Library/Logs/Claude/mcp-server-imap.log`. Logs never contain message content or addresses.

## Connect Claude Code or another MCP client

Any client that runs a stdio server can use the same command:

```bash
claude mcp add imap -- uv run --directory ~/src/imap-mcp imap-mcp serve
```

## Prompt injection

An email can contain text written to steer the agent. imap-mcp cannot send mail, so the worst it can do on its own is write a draft. But if the same agent also has a tool that can reach the network (web fetch, HTTP, another mail tool), a hostile email can tell the agent to leak data through that tool. In a session that reads mail, give the agent as few network tools as possible.

## Tools

| Tool | Effect |
|---|---|
| `search_mail` | Ranked keyword search with filters. Skips Junk and Trash unless named. |
| `get_message` | One message: headers, plain-text body, attachment list |
| `list_folders` | Folders with message and unread counts |
| `sync_now` | Sync now, within the time limit |
| `download_attachment` | Save one attachment to `download_dir` |
| `create_draft` | Write a Markdown draft to the Drafts folder. Never sends. |

## Commands

| Command | Effect |
|---|---|
| `imap-mcp serve` | Run the MCP server on stdio |
| `imap-mcp set-password` | Store the app password in the Keychain |
| `imap-mcp probe` | Log in and print capabilities and folders |
| `imap-mcp sync` | Run a full sync with no time limit |
| `imap-mcp status` | Show sync state, folder counts and database size |
| `imap-mcp vacuum` | Rebuild the database file |
| `imap-mcp purge --yes` | Delete the index |

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run pyright && uv run mypy && uv run pytest
```

Tests never connect to a real mailbox. They use a TLS fake IMAP server that records every command and reproduces the iCloud quirks. Read [AGENTS.md](AGENTS.md) before you change code.

## Docs

- [Design](docs/designs/imap-mcp.md): goals, security model, sync design and decisions
- [API reference](docs/api/index.html): generated from the docstrings with `pdoc`

## Licence

MIT. See [LICENSE](LICENSE).
