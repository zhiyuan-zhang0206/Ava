"""Reference watcher: watch a PR's CI and wake the agent once it settles.

One-shot — delivers exactly one wake message, then exits. Launch it from an
agent-profile process carrying the runner DB and Redis URLs with
`ava.watcher.launch(code, timeout=..., name="ci-watch-<pr>")` right after
pushing a PR; the wake message carries the verdict.

Delivery survives a restart window: the settled verdict is persisted first —
`ci-verdict-<PR>.txt` in the launching agent's workspace — then delivered
with a bounded retry (gaps doubling from 10s to a 160s cap, ~10.5 min in
total).  A gateway / agent restart window (an update wave, `ava cluster
update`) refuses connections for minutes and outlasts the SDK's own 3 quick
send retries; without persistence + retry a wake landing in that window is
lost with nothing left behind. A persistent agent-profile owner-URL config
error stops after one attempt. Exit codes: 0 the wake was delivered, 1 the CI
probe raised, 2 delivery failed — the absolute verdict path and cause are in
the log and exit notice, and the persisted file is the fallback record.

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
- **NO_CHECKS is re-polled before it is reported** — the rollup is also empty
  during the attach window right after a push (checks take a moment to appear,
  and a run that has not registered yet is invisible to the runs-API probe
  too), so the first NO_CHECKS is not yet evidence of "nothing scheduled". Up
  to NO_CHECKS_RETRIES consecutive NO_CHECKS verdicts are retried before the
  agent is woken (task #3158).
- **MERGE_CONFLICT is NOT green** — the agent must rebase before CI can start.
- **gh needs a git repo context** — watcher processes run from the agent
  workspace, so the template `os.chdir`s into the repo before polling.

Usage:
1. Read this file with `ava.files.read(...)`
2. Replace the placeholders (REPO_ROOT / PR_NUMBER / CI_UTILS / WATCHER_ID)
3. Launch from an agent-profile process with `ava.watcher.launch(code,
   timeout="3h", name="ci-watch-<pr>")`
4. When no wake arrives, read `ci-verdict-<PR>.txt` in your workspace — it
   was already persisted before the watcher tried to deliver.
"""

import atexit
import os
import sys
import time

from pydantic import ValidationError

import ava
from shared.config.data_plane import AgentProfileOwnerDbUrlRefusedError
from shared.paths import workspace_dir

# ── Configure before launching ───────────────────────────────────────────────
REPO_ROOT = ""  # e.g. "/home/user/ava/.worktrees/ava-1234-task" — the worktree
# (or checkout) the PR branch is on. gh resolves the repo from
# cwd, so the watcher chdirs here before every poll.
PR_NUMBER = ""  # e.g. "1234"
CI_UTILS = ""  # e.g. "/home/user/ava/scripts" — directory containing ci_utils.py
CHECK_EVERY = 60  # seconds between polls
NO_CHECKS_RETRIES = 3  # consecutive NO_CHECKS verdicts tolerated before waking
TIMEOUT_S = 7200  # hard stop; reports "timed out" instead of a verdict
WATCHER_ID = 0  # agent to wake (ava.self.AGENT_ID of the launching agent)
WAKE_ATTEMPTS = 8  # wake delivery tries (first + 7 retries); the gaps below
# sum to ~10.5 min — long enough to ride out a gateway / agent restart window
# (wake-delivery retry contract; task #3696 exception inventory)
WAKE_BACKOFF_S = 10.0  # first gap between wake tries; doubles per retry
WAKE_BACKOFF_MAX_S = 160.0  # cap for one gap
VERDICT_FILE = f"ci-verdict-{PR_NUMBER}.txt"  # relative — `ava.files` resolves
# it in the launching agent's workspace: the settled verdict is persisted
# there before any delivery attempt and stays behind after a failed delivery
VERDICT_PATH = (workspace_dir(WATCHER_ID) / VERDICT_FILE).resolve()

os.chdir(REPO_ROOT)
sys.path.insert(0, CI_UTILS)
from ci_utils import CIStatus, check_ci  # noqa: E402


def persistent_config_cause(exc: Exception) -> str | None:
    """Recognize the owner-URL guard through Pydantic's ValidationError wrapper."""
    if not isinstance(exc, ValidationError):
        return None
    for issue in exc.errors(include_input=False):
        cause = issue.get("ctx", {}).get("error")
        if isinstance(cause, AgentProfileOwnerDbUrlRefusedError):
            return str(cause)
    return None


def wake(message: str) -> tuple[bool, int, str | None]:
    """Deliver `message` to the agent, retrying across a restart window.

    The SDK already retries 3 times, but a gateway restart window refuses
    connections for minutes: the growing gaps below ride it out. The owner-URL
    guard is persistent, so it returns after one attempt. Other errors retain
    the full retry window. Return delivery, attempts, and failure cause.
    """
    delay = WAKE_BACKOFF_S
    for attempt in range(1, WAKE_ATTEMPTS + 1):
        try:
            ava.agents.send_message(WATCHER_ID, message)
            return True, attempt, None
        except Exception as exc:
            config_cause = persistent_config_cause(exc)
            cause = config_cause or f"{type(exc).__name__}: {exc}"
            print(f"wake attempt {attempt}/{WAKE_ATTEMPTS} failed: {cause}", flush=True)
            if config_cause is not None:
                return False, attempt, cause
            if attempt < WAKE_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, WAKE_BACKOFF_MAX_S)
    return False, WAKE_ATTEMPTS, cause


def finish(message: str) -> bool:
    """Persist the terminal message, echo it to the session log, wake the agent.

    Persist-first is the contract: the record must exist even when every
    delivery attempt fails.  The persist is best-effort (its failure is
    echoed and the message still goes out); the wake is not — the return
    value is its outcome, and callers exit non-zero when it is False.
    """
    persisted = True
    try:
        ava.files.write(VERDICT_FILE, message + "\n")
    except Exception as exc:  # the wake below is the primary delivery path
        persisted = False
        print(f"verdict persist failed: {exc!r}", flush=True)
    print(message, flush=True)
    delivered, attempts, cause = wake(message)
    if not delivered:
        if persisted:
            fallback = f"the verdict is at {VERDICT_PATH}"
        else:
            fallback = (
                f"the verdict was not persisted at {VERDICT_PATH} — the message above is the record"
            )
        summary = f"wake delivery failed after {attempts} attempts — cause: {cause}; {fallback}"
        print(summary, flush=True)
        # The generated watcher boot may log a registry-cleanup traceback after
        # this script exits. Repeat the summary last so --tail-file includes
        # both the absolute fallback path and cause in the shell exit notice.
        if __name__ == "__main__":
            atexit.register(print, summary, flush=True)
    return delivered


start = time.time()
no_checks_retries = 0
while time.time() - start < TIMEOUT_S:
    try:
        status = check_ci(PR_NUMBER)
    except Exception as e:  # gh / network / JSON failure — report, do not hang
        # Exit 1 — the probe itself failed; persist + echo happen first, so
        # the error survives even when the wake cannot land (exit 2 is for
        # delivery-only failures).
        finish(f"PR #{PR_NUMBER} CI watcher error: {type(e).__name__}: {e}")
        raise SystemExit(1) from None

    verdict = status.verdict
    if verdict == CIStatus.PENDING:
        no_checks_retries = 0  # checks are attached — a later NO_CHECKS is a new window
        time.sleep(CHECK_EVERY)
        continue

    if verdict == CIStatus.NO_CHECKS and no_checks_retries < NO_CHECKS_RETRIES:
        # The first poll after a push can land inside the attach window: the
        # rollup is empty and the run may not even be registered yet, so this
        # NO_CHECKS is not yet evidence that nothing was scheduled. Re-poll a
        # bounded number of times before treating it as settled (task #3158).
        no_checks_retries += 1
        time.sleep(CHECK_EVERY)
        continue

    # Settled — wake the agent with the full picture. FAILED, MERGE_CONFLICT,
    # NO_WORKFLOW_RUNS, ERROR and NO_CHECKS (retry budget spent) all land here;
    # only PENDING loops.
    lines = [
        f"PR #{PR_NUMBER} CI settled: {verdict.value}",
        f"mergeable: {status.mergeable}",
    ]
    if verdict == CIStatus.NO_CHECKS and no_checks_retries:
        lines.append(
            f"re-polled {no_checks_retries} times over ~"
            f"{no_checks_retries * CHECK_EVERY}s and the rollup stayed empty"
        )
    if status.failed:
        failed_names = ", ".join(c.get("name", "?") for c in status.failed)
        lines.append(f"failed checks: {failed_names}")
    if status.passed:
        lines.append(f"passed checks: {', '.join(status.passed)}")
    if status.error_detail:
        lines.append(f"error detail: {status.error_detail}")
    if not finish("\n".join(lines)):
        raise SystemExit(2)
    break
else:
    message = (
        f"PR #{PR_NUMBER} CI watcher timed out after {TIMEOUT_S}s — still pending, investigate."
    )
    if not finish(message):
        raise SystemExit(2)
