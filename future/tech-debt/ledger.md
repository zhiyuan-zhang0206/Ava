# Tech-debt ledger

> Living tech-debt ledger. Current inspection rules and automated owners live in
> [technical-debt guidance](../../docs/conventions/engineering/tech-debt.md).
> The single "what debt is open now" view. Forward-looking (what we intend to
> fix) → lives in `future/`. Entries are agent + human maintained: humans
> set `wontfix`, and the sweeper must never re-add a `wontfix` item. Resolved
> items are deleted, not archived here — resolution history lives in git.

This is the single register for debt from every mechanism; do not create a
parallel ledger. A new mechanism, including a cap-domain "exit" registration,
records an ordinary fingerprinted entry here with its own class. PRs that
knowingly introduce debt register it here through the PR template, and a
requested reconciliation checks this ledger.

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

## Retired guarantees

The PTY whole-tree disappearance/proof requirement is retired by the user
[decision of 2026-10-07](../../docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md).
Do not reopen it as an implementation defect or infer it was fixed by passing
reruns. Historical Bash/TERM and OS-stall observations remain evidence for
operational investigation; this decision does not establish their root causes.
The job-readiness and interactive-write entries below retain their narrower
fixture/incident scope.

## Open

### boundary:supervisor-intent-fixture-stop-window
- **class**: boundary
- **status**: open
- **evidence**: The original `services/supervision/ava_root/tests/test_ava_root_intent.py:_supervisor` fixture coupled up-half intent tests to a 0.2s real-process stop budget. In `test_restart_up_half_failure_retry_replaces_and_clears`, attempt 1 of [PR #4321, run 37318714866, shard 10](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/37318714866/job/111792197420) exhausted that window on the first `await owner.restart("svc")` (before the intentional up-half seal failure) in `services/supervision/ava_root/stopping.py:_stop_posix_generation`. The loop classified leader PID 10633 as living before tree capture and durable custody I/O; the trace does not establish its state after that I/O at the deadline. It retained ownership rather than certifying closure without evidence. The next configured attempt passed this test; it failed a separate single-box port fixture instead. The three up-half/episode tests now terminate their captured disposable sleeper and await the real watcher before calling the unchanged stop boundary; input-seal failure, durable intent, episode clearing and fresh generation identity remain exercised. Restoring the old fixture executes a real live TERM stop and fails the regression's dependency assertion after the expected seal failure and retry; the spy delegates to the unchanged implementation rather than injecting a stop failure. Native down-refusal and `test_ava_root_stop_window.py` retain real TERM/deadline coverage. A separate native gate-controlled regression demonstrates a stale-read defect: a captured TERM-handling child exits during delegated custody I/O, but the old expiry branch refuses using its earlier live snapshot. The stop now rechecks captured births at expiry and still requires the real watcher and empty-group proof before releasing custody; a fresh live birth or unproven group retains the existing refusal. The regression fails the old implementation and passes the repair without increasing the stop window. This does not identify the historical PID 10633 cause, which remains open: that trace does not establish CPU load or signal-delivery failure. Diagnose that incident before changing its timeout or claiming its root cause resolved.
- **first-seen**: 2026-10-05 (PR #4321)
- **last-verified**: 2026-10-06


### boundary:pty-kill-verdict-job-readiness
- **class**: boundary
- **status**: open
- **evidence**: In [PR #4356, run 37405453654, shard 14](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/37405453654/job/112082738845), the raw first-attempt JUnit report (`junit-backend-shard-14-a1.xml`) records `base/sessions/tests/test_pty_backend.py::test_kill_session_with_verdict_reports_whether_work_was_cut_short` receiving `(True, "forced", False)` after its busy-shell predicate saw a child. The job's configured retry passed; that is not a repair or proof of the first child's identity. The test waited for any child, not the intended `sleep 300`; `base/sessions/pty/tests/job_wait.py:wait_for_job` already documents transient login-shell helpers and matches actual argv. The backend test now waits for a bare ready output line and then that exact job, preserving its real kill/verdict assertion, using the same arrangement as `services/agent_runner/pty_sessions/tests/test_sessions.py::test_kill_a_session_with_work_reports_interrupted`. The missing intended-job precondition is repaired. The observed first-attempt child's identity was not captured, so the original failure's cause remains unproven; do not infer a production membership defect or close the Bash/TERM incidents from this change.
- **first-seen**: 2026-10-06 (PR #4356)
- **last-verified**: 2026-10-06


### boundary:interactive-bash-smoke-first-write
- **class**: boundary
- **status**: open
- **evidence**: `tests/skills/test_impersonation_launch.py::test_app_server_command_executes_in_an_interactive_bash` blocked on macOS at the first `os.write(master, export-PATH)` before submitting the app-server command. The unchanged main node also exceeded a 65-second bound with a fresh temporary `AVA_HOME`; a separate 25-second diagnostic with a 10-second faulthandler dump located that write. This stand-in pane is created with `pty.openpty` and `Popen(start_new_session=True)`, while the service uses `pty.fork`; that difference is an investigation lead, not a proven cause. A fresh macOS exact-node run on 2026-10-06 passed; this does not resolve the recorded hang. A native probe found both this fixture and production `fork_shell` had their shell PID as session leader and terminal foreground group after startup, disproving missing controlling-terminal ownership at that point. The existing master was blocking, the first export was 106 bytes, startup had reached the shell prompt, and that probe wrote all bytes in about 21 microseconds. Neither the startup/write ordering nor the earlier blocked write has a demonstrated root cause. The Linux CI check remains selected. Do not infer an app-server or production transport defect, add a timeout, or skip the check from this evidence.
- **first-seen**: 2026-10-05 (literal coding-session refresh)
- **last-verified**: 2026-10-06


### boundary:checkpoint-postgres-historical-walk-patch
- **class**: boundary
- **status**: open
- **evidence**: `base/agents/history/checkpoint_postgres_walks.py:install_checkpoint_postgres_walk_patch` wraps private `BasePostgresSaver._try_advance_walks` for checkpoint-postgres 3.1.2. It keeps an unseen historical target out of the cached walk cursor until a later page arrives; `base/agents/history/delta_read_compat.py` installs it for gateway and agent-host readers. The dependency version, method identity, and signature are guarded because this is a temporary third-party patch. Once langgraph#8448 / #8556 is released, run `base/agents/history/tests/test_checkpoint_postgres_walks.py`, remove the pagination wrapper (retaining the message-reset suffix reader until upstream supports it), then upgrade the checkpoint-postgres pin (405 tracks the follow-up).
- **first-seen**: 2026-09-23 (PR for task #4518)
- **last-verified**: 2026-09-23

### deps:playwright-1.62to1.63
- **class**: deps
- **status**: open
- **evidence**: `uv pip list --outdated` (synced worktree venv, 2026-09-23) reports playwright 1.62.0 -> 1.63.0; PyPI latest re-confirmed 1.63.0. `uv.lock` pins 1.62.0. Chromium follows the wheel — the CI jobs that need a browser install it with `uv run playwright install chromium` plus its system deps through the hardened `install-deps` step, cached on the `uv.lock` hash — so the bump is the wheel alone.
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
- **evidence**: ~18 lines (soft cap 12) after the 2026-09-27 watcher-registry removal (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md) dropped the old supersede/replace/reuse residue entirely. Residue now: five Args (cron format, timezone, end_time types, name, notify) + the one-line warning that re-registering the same schedule does NOT dedupe any more — it starts a second, independent session, whose loss would silently reintroduce the double-firing confusion the old dedupe used to prevent. The class's calibration note already expected `watcher.cron` as an Args-format-heavy standing item.
- **first-seen**: 2026-09-23
- **last-verified**: 2026-09-27
