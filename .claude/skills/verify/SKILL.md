---
name: verify
description: Run CCBot's quality gates (ruff lint, ruff format check, pyright, pytest with coverage, secret and debug-print scan, diff review) and produce a PASS/FAIL report. Use after finishing a feature, fix, or refactor, and before restarting the service, committing, or opening a PR.
license: MIT
metadata:
  origin: Adapted from ECC verification-loop (github.com/affaan-m/ECC)
---

# Verify

Runs the same checks as CI plus a few CCBot-specific scans, then reports whether
the change is ready. Run from the repository root.

## Phase 1: Lint and format

```bash
set -o pipefail
uv run ruff check src/ tests/ 2>&1 | sed -n '1,40p'
uv run ruff format --check src/ tests/ 2>&1 | sed -n '1,40p'
```

If formatting fails, run `uv run ruff format src/ tests/` and re-check. Do not
use black or isort; ruff is the only formatter.

## Phase 2: Type check

```bash
set -o pipefail
uv run pyright src/ccbot/ 2>&1 | tail -30
```

Must be 0 errors.

## Phase 3: Tests

```bash
set -o pipefail
uv run pytest --tb=short -q --cov=ccbot --cov-report=term-missing 2>&1 | tail -60
```

Report total, passed, failed, and coverage of the modules touched by the diff.
New or changed behavior must have a test. There is no hard coverage threshold,
but coverage of touched modules should not drop.

## Phase 4: CCBot scans

```bash
# Secrets or tokens committed by mistake
git diff HEAD --unified=0 | grep -nE '^\+.*([0-9]{8,10}:[A-Za-z0-9_-]{35}|TELEGRAM_BOT_TOKEN *= *[^ ]|sk-[A-Za-z0-9]{20,})' || true
# Debug prints in source (use logging instead)
grep -rnE '^\s*print\(' src/ccbot/ | grep -vE '^src/ccbot/(main|hook|file_transfer)\.py:' | head -20 || true
# Direct bot calls that bypass the MarkdownV2 fallback helpers in handlers
git diff HEAD --unified=0 -- src/ccbot/handlers | grep -nE '^\+.*\.(reply_text|send_message|edit_message_text)\(' || true
```

Treat any token match as CRITICAL. Prints and direct bot calls need a reason
(CLI output in `main.py`, `hook.py`, and the `ccbot files`/`send-file` commands in `file_transfer.py`, or queue code with its own fallback).

## Phase 5: Diff review

```bash
git status --short
git diff HEAD --stat
```

Check each changed file for unintended edits, a module docstring on new `.py`
files, callback data under 64 bytes, routing keyed by window ID rather than
window name, and a matching update in `docs/customizations.md` when fork
behavior changed.

## Target acceptance

Local checks do not prove the running bot works. When the change affects
runtime behavior and the user has authorized it, restart the service, read the
service log for startup errors, and ask the user to exercise the feature in
Telegram. Report that result separately. Restart at the end of the work, since
the session doing the restart loses notifications briefly.

## Report

```
VERIFICATION REPORT
===================
Lint:     PASS/FAIL
Format:   PASS/FAIL
Types:    PASS/FAIL (N errors)
Tests:    PASS/FAIL (X/Y passed, touched-module coverage Z%)
Scans:    PASS/FAIL (details)
Diff:     N files changed

Overall:  READY / NOT READY
Target:   VERIFIED / FAILED / NOT PERFORMED / NOT APPLICABLE (evidence)

Issues to fix:
1. ...
```
