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

[unreleased]: https://github.com/bcrisp4/imap-mcp/commits/main
