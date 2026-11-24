# Development Workflow

Adapted from ECC (github.com/affaan-m/ECC, MIT) for CCBot. The service runs this
checkout directly through an editable install, so every change is live after the
next restart. Work in small, verified steps.

## Feature and fix flow

1. **Read first.** Check `git status` and the branch, then read the relevant rule
   in `.claude/rules/` and the matching section of `docs/customizations.md`.
   Reuse existing helpers (`safe_reply`/`safe_edit`/`safe_send`, `split_message`,
   routing in `routing.py`) before writing new ones.
2. **Plan non-trivial work.** For changes touching routing, state files, hooks,
   or recovery, write a short plan first: affected modules, state migration,
   failure modes, and which tests prove it.
3. **Test first.** Write or extend a failing pytest that reproduces the bug or
   describes the behavior, then implement until it passes. Use the `tdd-guide`
   agent for larger features. Telegram is always mocked; tmux integration tests
   use their own socket and never touch the `ccbot` session.
4. **Verify.** Run the `verify` skill (ruff, format, pyright, pytest). All gates
   must pass before a restart or commit.
5. **Review.** After a meaningful diff, run `python-reviewer`. Add
   `security-reviewer` when the change touches auth, owner lock, file transfer,
   paths, subprocess/tmux input, or logging of user data. Add
   `silent-failure-hunter` when the change adds `except` blocks, retries, or
   fallbacks. Fix CRITICAL and HIGH findings before continuing.
6. **Document.** Behavior changes in the fork go into `docs/customizations.md`.
   Bump `+custom.N` in `pyproject.toml` when the change ships.

## Review severity

| Level | Meaning | Action |
|-------|---------|--------|
| CRITICAL | Security hole, lost or misrouted messages, corrupted state | Block |
| HIGH | Bug or swallowed error that hides a failure | Fix before commit |
| MEDIUM | Maintainability (long function, duplication, missing docstring) | Fix when cheap |
| LOW | Style nit | Optional |

## Commits

Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
`chore:`). One logical change per commit. Commit to `custom` (or a feature
branch merged into it) and push to `fork`, never to `origin`.
