"""Reference watcher: watch a PR's CI and wake the agent once it settles.

One-shot — sends exactly one message, then exits.  Launch it with
`ava.watcher.launch(code, timeout=..., name="ci-watch-<pr>")` right after
pushing a PR; the single message wakes you with the verdict.

Uses `scripts/ci_utils.py:check_ci` — the repo-provided, correct CI polling
logic.  Do NOT write ad-hoc `gh pr checks` + exit-code checks: `gh pr checks`
exits non-zero when a check FAILS, so a `returncode == 0` condition silently
drops the red case and the watcher only wakes on the timeout — exactly the bug
this template exists to prevent.

Pitfalls this template covers (all from ci_utils.check_ci's verdict, not from
raw gh output):

- **FAILED wakes the agent** — a red PR is a settled verdict, never a timeout.
- **PENDING is never green** — only a non-PENDING verdict wakes the agent.
- **NO_WORKFLOW_RUNS is NOT green** — Actions never scheduled; the agent gets
  the verdict and must find out why the workflow did not run.
- **MERGE_CONFLICT is NOT green** — the agent must rebase before CI can start.
- **gh needs a git repo context** — watcher processes run from the agent
  workspace, so the template `os.chdir`s into the repo before polling.

Usage:
1. Read this file with `ava.files.read(...)`
2. Replace the placeholders (REPO_ROOT / PR_NUMBER / CI_UTILS / WATCHER_ID)
3. Launch with `ava.watcher.launch(code, timeout="3h", name="ci-watch-<pr>")`
4. The watcher's single message wakes you
"""

import os
import sys
import time

import ava

# ── Configure before launching ───────────────────────────────────────────────
REPO_ROOT = ""  # e.g. "/home/user/ava/.worktrees/ava-1234-task" — the worktree
# (or checkout) the PR branch is on. gh resolves the repo from
# cwd, so the watcher chdirs here before every poll.
PR_NUMBER = ""  # e.g. "1234"
CI_UTILS = ""  # e.g. "/home/user/ava/scripts" — directory containing ci_utils.py
CHECK_EVERY = 60  # seconds between polls
TIMEOUT_S = 7200  # hard stop; reports "timed out" instead of a verdict
WATCHER_ID = 0  # agent to wake (ava.self.AGENT_ID of the launching agent)

os.chdir(REPO_ROOT)
sys.path.insert(0, CI_UTILS)
from ci_utils import CIStatus, check_ci  # noqa: E402


def wake(message: str) -> None:
    ava.agents.send_message(WATCHER_ID, message)


start = time.time()
while time.time() - start < TIMEOUT_S:
    try:
        status = check_ci(PR_NUMBER)
    except Exception as e:  # gh / network / JSON failure — report, do not hang
        wake(f"PR #{PR_NUMBER} CI watcher error: {type(e).__name__}: {e}")
        raise SystemExit(1) from None

    verdict = status.verdict
    if verdict == CIStatus.PENDING:
        time.sleep(CHECK_EVERY)
        continue

    # Settled — wake the agent with the full picture. FAILED, MERGE_CONFLICT,
    # NO_WORKFLOW_RUNS, NO_CHECKS and ERROR all land here; only PENDING loops.
    lines = [
        f"PR #{PR_NUMBER} CI settled: {verdict.value}",
        f"mergeable: {status.mergeable}",
    ]
    if status.failed:
        failed_names = ", ".join(c.get("name", "?") for c in status.failed)
        lines.append(f"failed checks: {failed_names}")
    if status.passed:
        lines.append(f"passed checks: {', '.join(status.passed)}")
    if status.error_detail:
        lines.append(f"error detail: {status.error_detail}")
    wake("\n".join(lines))
    break
else:
    wake(f"PR #{PR_NUMBER} CI watcher timed out after {TIMEOUT_S}s — still pending, investigate.")
