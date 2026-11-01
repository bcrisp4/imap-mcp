# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MCP server over stdio with six tools: `search_mail`, `get_message`, `list_folders`, `sync_now`, `download_attachment` and `create_draft`.
- Local SQLite index with ranked FTS5 keyword search. Search skips Junk and Trash unless the agent names the folder.
- Sync on a tool call when the data is more than 15 minutes old, with a 20 second time limit, and `sync_now` to force one. Every result reports how old the data is.
- Incremental sync: `STATUS` checks, `CONDSTORE` flag updates, moves linked without a download, and `UIDVALIDITY` relinks that resume after an interruption.
- A cross-process sync lock, so several agent sessions can share one index.
- An IMAP command allowlist that refuses every write except `APPEND` to the Drafts folder.
- Prompt-injection defences: sender text kept apart from server data, nonce-tagged message text, hidden HTML and invisible Unicode removed, and sender authentication results.
- Markdown drafts with raw HTML off and images rendered as links.
- Attachment download that never overwrites a file and sets the macOS quarantine attribute.
- The macOS Keychain as the password store.
- CLI commands: `serve`, `set-password`, `probe`, `sync`, `status`, `vacuum` and `purge`.
- The `auth_servers` setting names the domains whose `Authentication-Results` headers count.

### Security

- Authentication verdicts come only from the top headers whose authserv-id is in `auth_servers`, and only from the `method=result` token at the start of each clause. Sender text in a comment or in `smtp.mailfrom` can no longer add a pass verdict.
- The message identity includes the RFC822 size, so a forged copy with a real message's `Message-ID`, `Date` and `From` no longer merges into it.
- A message the client cannot parse, such as one nested hundreds of MIME levels deep, is skipped. It no longer stops the sync or makes every read tool fail.
- IMAP error messages no longer contain library error text, which could quote headers into logs and the `sync.error` field.
- `attachments[].type` shows only known media types.
- `create_draft` and `download_attachment` carry `openWorldHint: true`.
- Draft headers hold the parsed address that `new_recipients` reports.
- Attachment names are capped at 255 UTF-8 bytes.
- CI actions are pinned to commit SHAs, and uv and hatchling to exact versions.

[unreleased]: https://github.com/bcrisp4/imap-mcp/commits/main
