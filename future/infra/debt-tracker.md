# Debt tracker

> Living tech-debt tracker, maintained by the sweeper engine (`ava_builtins/skills/sweeper/`)
> driven by this repo's debt classes (`.agents/skills/ava-sweeper/`).
> The single "what debt is open now" view. Forward-looking (what we intend to
> fix) → lives in `future/`. Entries are agent + human maintained: humans
> set `wontfix`, and the sweeper must never re-add a `wontfix` item. Resolved
> items are deleted, not archived here — resolution history lives in git.

<!-- watermark: last-swept-sha=6882acc0 last-swept-date=2026-06-03 -->

## Entry format

Each open or wontfix item is one `###` block keyed by a stable fingerprint, so a
reconcile pass can match it across runs:

    ### <fingerprint>
    - **class**: deps | docs-aging | fail-fast | inline-marker | dead-code | boundary | skill-desc | docstring-budget
    - **status**: open | wontfix
    - **evidence**: file:line refs / command output; for `boundary`, the named
      files+symbols and one line on why it is a smell
    - **first-seen**: YYYY-MM-DD (PR #NNN)
    - **last-verified**: YYYY-MM-DD

Fingerprint convention: `<class>:<file>:<symbol>` for localized classes; a short
human-readable `<class>:<slug>` for `boundary` (spans files, no single symbol).

## Open

### deps:playwright-1.59to1.60
- **class**: deps
- **status**: open
- **evidence**: `uv pip list --outdated` — playwright 1.59.0 -> 1.60.0 (+ pytest-playwright 0.7.2 -> 0.8.0). NOT a lock-only bump: the e2e CI job runs natively on the CI host, whose chromium lives at `/opt/ms-playwright/` (installed by `scripts/provision/install-playwright.sh`, `PLAYWRIGHT_VERSION` pinned). Bumping the `playwright` wheel without refreshing that browser makes `uv sync --frozen` install a wheel whose expected browser revision is absent on disk ("Executable doesn't exist at .../chromium_headless_shell-..."). Its PR must, in lockstep: bump the wheel + `PLAYWRIGHT_VERSION` in `install-playwright.sh` (re-run on the CI host) AND in `Dockerfile` (rebuild the eval image).
- **first-seen**: 2026-06-03 (backend routine batch — e2e browser mismatch)
- **last-verified**: 2026-06-03

## Wontfix

_(none)_
