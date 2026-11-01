# Releasing

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0, a minor version can change a tool interface. A tag `vMAJOR.MINOR.PATCH` starts the release workflow (`.github/workflows/release.yml`).

## Steps

1. Pick the version. Increase MINOR for new features or a tool interface change, PATCH for fixes only.

2. Set the version in `pyproject.toml`:

   ```bash
   uv version 0.2.0
   ```

3. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.2.0] - YYYY-MM-DD` with today's date, then add a new empty `## [Unreleased]` above it.

4. Update the link references at the end of `CHANGELOG.md`:

   ```markdown
   [unreleased]: https://github.com/bcrisp4/imap-mcp/compare/v0.2.0...HEAD
   [0.2.0]: https://github.com/bcrisp4/imap-mcp/compare/v0.1.0...v0.2.0
   ```

   For the first release, use `[0.1.0]: https://github.com/bcrisp4/imap-mcp/releases/tag/v0.1.0`.

5. Commit the two files on a branch, push it and open a pull request:

   ```bash
   git switch -c release/0.2.0 && git commit -S -am "chore: release 0.2.0"
   ```

   ```bash
   git push -u origin release/0.2.0 && gh pr create --fill
   ```

6. When the `check` job passes, squash-merge the pull request. The `main` ruleset accepts only squash merges, which GitHub signs.

   ```bash
   gh pr merge --squash --delete-branch
   ```

7. Update `main`, then create a signed tag on the merge commit:

   ```bash
   git switch main && git pull --ff-only
   ```

   ```bash
   git tag -s v0.2.0 -m "v0.2.0"
   ```

8. Push the tag. The `release tags` ruleset lets only repository admins create `v*` tags.

   ```bash
   git push origin v0.2.0
   ```

9. Approve the `pypi` environment when GitHub asks. The workflow then publishes to PyPI and creates the GitHub release with the changelog section as its notes.

## What the workflow checks

- CI passes on the tagged commit.
- The tag equals `v` plus the version in `pyproject.toml`.
- `CHANGELOG.md` has a dated, non-empty section for the version.

If a check fails, nothing is published. Fix the cause, delete the tag locally and on GitHub, and tag again. The `release tags` ruleset blocks moving or deleting a `v*` tag for everyone except admins. After a PyPI upload succeeds, the version number is used for ever. A broken release needs a new PATCH version.
