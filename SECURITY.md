# Security policy

## Report a vulnerability

Do not open a public issue for a security problem. Report it through [GitHub private vulnerability reporting](https://github.com/bcrisp4/imap-mcp/security/advisories/new).

Include the version, the steps to reproduce, and the impact.

## Supported versions

Only the latest release gets security fixes.

## Scope

These problems are in scope:

- An IMAP command that the allowlist in `src/imap_mcp/adapters/imap/guard.py` does not stop.
- Sender-controlled text in a trusted result field.
- A path from email content to a send, delete, move or flag action.
- A log line that contains message content, an address or a password.

The security model is in the [design doc](docs/designs/imap-mcp.md#security).
