# Tech-debt ledger

> Living tech-debt ledger, maintained by the sweeper engine (`ava_builtins/skills/sweeper/`)
> driven by this repo's debt classes (`.agents/skills/ava-sweeper/`).
> The single "what debt is open now" view. Forward-looking (what we intend to
> fix) → lives in `future/`. Entries are agent + human maintained: humans
> set `wontfix`, and the sweeper must never re-add a `wontfix` item. Resolved
> items are deleted, not archived here — resolution history lives in git.

This is the single register for debt from every mechanism; do not create a
parallel ledger. A new mechanism, including a cap-domain "exit" registration,
records an ordinary fingerprinted entry here with its own class. PRs that
knowingly introduce debt register it here through the PR template, and the
daily debt-clearing pass reconciles this ledger.

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
Mechanisms may add structured bullet fields beyond this base set (for example,
cap-domain exits use `exitType`, `expires`, and `approver`); readers ignore unknown fields.

## Open

### deps:playwright-1.59to1.60
- **class**: deps
- **status**: open
- **evidence**: `uv pip list --outdated` — playwright 1.59.0 -> 1.60.0 (+ pytest-playwright 0.7.2 -> 0.8.0). NOT a lock-only bump: the e2e CI job runs natively on the CI host, whose chromium lives at `/opt/ms-playwright/` (installed by `scripts/provision/install-playwright.sh`, `PLAYWRIGHT_VERSION` pinned). Bumping the `playwright` wheel without refreshing that browser makes `uv sync --frozen` install a wheel whose expected browser revision is absent on disk ("Executable doesn't exist at .../chromium_headless_shell-..."). Its PR must, in lockstep: bump the wheel + `PLAYWRIGHT_VERSION` in `install-playwright.sh` (re-run on the CI host) AND in `Dockerfile` (rebuild the eval image).
- **first-seen**: 2026-06-03 (backend routine batch — e2e browser mismatch)
- **last-verified**: 2026-06-03

### deps:playwright-1.62to1.63
- **class**: deps
- **status**: open
- **evidence**: Worktree mechanical scan (2026-09-21) reports playwright 1.62.0 -> 1.63.0. `uv.lock` pins 1.62.0 while `Dockerfile:29` and `scripts/provision/install-playwright.sh:10` still provision Chromium with PLAYWRIGHT_VERSION=1.59.0. This is actionable high-risk dependency debt: upgrade the wheel and both browser-provision pins together, then refresh the CI host and eval image.
- **first-seen**: 2026-09-21
- **last-verified**: 2026-09-21

### boundary:ava-watcher-py-split
- **class**: boundary
- **status**: open
- **evidence**: `ava/watcher.py` sits exactly at the 800-line hard ceiling (per-file budget in `conventions/python-conventions.md`: 600 soft / 800 hard, enforced by `scripts/lint_code_structure.py`; no exemption — split is the prescribed remedy). Both R2 PRs (#3148, #3150) had to compress/offload content to stay under the cap. Candidate split: spawn/rebuild helpers vs. public API surface.
- **first-seen**: 2026-09-22 (PR #3150)
- **last-verified**: 2026-09-22

## Wontfix

_(none)_
