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

<!-- watermark: last-swept-sha=a2495ca1f last-swept-date=2026-09-23 -->

## Entry format

Each open or wontfix item is one `###` block keyed by a stable fingerprint, so a
reconcile pass can match it across runs:

    ### <fingerprint>
    - **class**: deps | docs-aging | fail-fast | inline-marker | dead-code | boundary | skill-desc | docstring-budget | locality
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

### locality:cli/commands/agents/control.py:cli/parsers/agents.py
- **class**: locality
- **status**: open
- **evidence**: `cochange.py`: c=11, confidence 79%; same shape for `cli/commands/agents/impersonation.py` (c=11, 79%) and `impersonation_relay.py` (c=10, 71%) against `cli/parsers/impersonation.py`. Leaked decision: each command's argument surface lives twice — argparse flags in `cli/parsers/`, the parameters they feed in the `cmd_*` implementation: `2095b997a` (`ava impersonate send`) and `5b9f770b9` (caller attestation) edited both. Fix direction: each command domain owns its settings-free parser next to its commands (the `cli.commands` package door is already empty); waits for #3479, which rewrites `cli/main.py` and `cli/parsers/__init__.py`.
- **first-seen**: 2026-09-28 (locality class first run)
- **last-verified**: 2026-09-28

### boundary:checkpoint-postgres-historical-walk-patch
- **class**: boundary
- **status**: open
- **evidence**: `base/agents/history/checkpoint_postgres_walks.py:install_checkpoint_postgres_walk_patch` wraps private `BasePostgresSaver._try_advance_walks` for checkpoint-postgres 3.1.2. It keeps an unseen historical target out of the cached walk cursor until a later page arrives; `base/agents/history/delta_read_compat.py` installs it for gateway and agent-host readers. The dependency version, method identity, and signature are guarded because this is a temporary third-party patch. Once langgraph#8448 / #8556 is released, run `base/agents/history/tests/test_checkpoint_postgres_walks.py`, remove the pagination wrapper (retaining the message-reset suffix reader until upstream supports it), then upgrade the checkpoint-postgres pin (405 tracks the follow-up).
- **first-seen**: 2026-09-23 (PR for task #4518)
- **last-verified**: 2026-09-23

### deps:playwright-1.62to1.63
- **class**: deps
- **status**: open
- **evidence**: `uv pip list --outdated` (synced worktree venv, 2026-09-23) reports playwright 1.62.0 -> 1.63.0; PyPI latest re-confirmed 1.63.0. `uv.lock` pins 1.62.0. Chromium follows the wheel — the CI jobs that need a browser install it with `uv run playwright install --with-deps chromium`, cached on the `uv.lock` hash — so the bump is the wheel alone.
- **first-seen**: 2026-09-21
- **last-verified**: 2026-09-23

## Wontfix

### docstring-budget:ava/security.py:module
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: module docstring trimmed 15 -> 6 lines this pass (soft cap 2). Kept: the module is off the default prompt surface (not in `__all_for_ava__`, not in the SDK-expand list), so the standing prompt pays nothing; the residue is the purpose + the mitigation caveat, for `help()` drill-down readers. Cutting further would drop the non-boundary warning.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-23

### docstring-budget:ava/ui.py:serve
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: trimmed 31 -> 20 lines this pass (soft cap 12). The residue is contract: five Args each carrying a real constraint (dir resolution, name charset, port range/policy, title/ttl defaults) plus the session life-cycle and `index.html` requirements — the class's Args-format-heavy allowance.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-23

### docstring-budget:ava/watcher.py:cron
- **class**: docstring-budget
- **status**: wontfix
- **evidence**: ~18 lines (soft cap 12) after the 2026-09-27 watcher-registry removal (decisions/2026-09-27-watchers-are-never-restarted.md) dropped the old supersede/replace/reuse residue entirely. Residue now: five Args (cron format, timezone, end_time types, name, notify) + the one-line warning that re-registering the same schedule does NOT dedupe any more — it starts a second, independent session, whose loss would silently reintroduce the double-firing confusion the old dedupe used to prevent. The class's calibration note already expected `watcher.cron` as an Args-format-heavy standing item.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-27
