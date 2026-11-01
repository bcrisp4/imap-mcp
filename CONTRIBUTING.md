# Contributing

Thank you for your help. imap-mcp is a small project with one maintainer, so keep changes small and focused.

## Before you start

- If you want to make a large change or a new feature, open an issue first. This prevents work on a change that the project cannot accept.
- Read the [design doc](docs/designs/imap-mcp.md). It is the source of truth. Its "Closed issues" section lists decisions that the project does not reopen without new evidence.
- Read [AGENTS.md](AGENTS.md). It lists the code layout, the rules, and what to read before you change each part.

## Hard rules

Pull requests that break these rules cannot be merged:

- Add no SMTP code and no send feature.
- Send no IMAP command outside the allowlist in `src/imap_mcp/adapters/imap/guard.py`.
- Put no sender-controlled text in a trusted result field.
- Log no subject, body, address, snippet, attachment name, search query or password.
- Do not connect to a real mailbox from a test. Use the fake server in `tests/imapserver/`.
- Add no dependency with a GPL-family licence.

AGENTS.md gives the reason for each rule.

## Set up

You need macOS and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/bcrisp4/imap-mcp
```

```bash
cd imap-mcp && uv sync
```

## Run the checks

Run these before you push. CI runs the same checks on macOS:

```bash
uv run ruff check && uv run ruff format --check && uv run pyright && uv run mypy && uv run pytest
```

If you change a dependency, also run the licence check:

```bash
uv run --no-dev python scripts/check_licenses.py
```

## Pull requests

1. Make a branch from `main`.
2. Add a test if your change fixes a bug or adds behaviour that callers rely on.
3. If your change is visible to users, add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
4. Write the pull request title as a [Conventional Commit](https://www.conventionalcommits.org/), for example `fix: keep one bad message from stopping the sync`.
5. Open the pull request against `main`.

The `check` CI job must pass. The maintainer merges by squash, so the pull request title becomes the commit message on `main`.

## Security issues

Do not open a public issue for a security problem. Report it through [GitHub private vulnerability reporting](https://github.com/bcrisp4/imap-mcp/security/advisories/new).

## Licence

By contributing, you agree that your contribution is licensed under the [MIT licence](LICENSE).
