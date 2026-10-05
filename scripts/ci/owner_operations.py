"""Optional owner operations: queue submission, eviction and GitHub job re-runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from math import ceil

from scripts.ci import status, trunk_api
from scripts.ci.job_rerun import CiJobRerunError, list_failed_jobs, rerun_failed_jobs
from scripts.ci.monitor import _deadline_hit
from scripts.ci.status import MAX_CONSECUTIVE_ERRORS, CIStatus, _parse_ts

QUEUE_COOLDOWN_SECONDS = 300
RETRY_BACKOFF_SECONDS = 300
_QUEUE_CHOICES = ("trunk",)
_QUEUE_NAMES = frozenset(_QUEUE_CHOICES)
_TRUNK_PRIORITIES = {"urgent": 0, "high": 10, "medium": 100, "low": 200}


def _resolve_queue(queue: str | None) -> str:
    """Return the CLI-selected queue, then CI_QUEUE, then the Trunk default."""
    resolved = queue or os.environ.get("CI_QUEUE") or "trunk"
    if resolved not in _QUEUE_NAMES:
        raise ValueError(f"unknown CI queue: {resolved}")
    return resolved


def _trunk_priority(priority: str) -> int:
    """Map the user-facing Trunk priority to its REST API integer."""
    return _TRUNK_PRIORITIES[priority]


def _last_head_update(pr: str, repo: str) -> float | None:
    """Best-effort newest force-push event or head-commit committer date."""
    probes = (
        (
            f"repos/{repo}/issues/{pr}/timeline",
            "--paginate",
            '[.[] | select(.event == "head_ref_force_pushed")] | .[-1].created_at',
        ),
        (f"repos/{repo}/pulls/{pr}/commits", "", ".[-1].commit.committer.date"),
    )
    timestamps: list[float] = []
    for endpoint, paginate, jq in probes:
        r = subprocess.run(  # noqa: S603
            ["gh", "api", endpoint, *((paginate,) if paginate else ()), "--jq", jq],
            capture_output=True,
            text=True,
            check=False,
        )
        parsed = _parse_ts(r.stdout) if r.returncode == 0 else None
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps, default=None)


def _queue_cooldown_seconds(pr: str, repo: str) -> int:
    last = _last_head_update(pr, repo)
    return 0 if last is None else max(0, ceil(QUEUE_COOLDOWN_SECONDS - (time.time() - last)))


def _submit_trunk(pr: str, repo: str, priority: str, *, token: str) -> int:
    """Submit a green PR to Trunk, retrying one failed submission."""
    payload = trunk_api.pr_payload(pr, repo)
    payload.update({"priority": _trunk_priority(priority), "noBatch": False})
    for attempt in range(2):
        _, error = trunk_api.post("submitPullRequest", payload, token)
        if error is None:
            print(f"PR #{pr} submitted to the Trunk merge queue", file=sys.stderr, flush=True)
            return 0
        if error == "HTTP 409":
            print(
                f"PR #{pr} already in the Trunk merge queue — resuming watch",
                file=sys.stderr,
                flush=True,
            )
            return 0
        print(
            f"[ci] Trunk queue submit error ({attempt + 1}/2): {error}",
            file=sys.stderr,
            flush=True,
        )
        if attempt == 1:
            print(f"PR #{pr} Trunk queue submission failed", file=sys.stderr, flush=True)
            return 4
        print(
            f"retrying Trunk queue submission in {RETRY_BACKOFF_SECONDS}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(RETRY_BACKOFF_SECONDS)
    return 4


def _trunk_cancel(pr: str, repo: str, *, token: str) -> int:
    """Evict a PR from the Trunk queue; 0 cancelled, 1 not in queue, 4 error.
    Trunk exposes `cancelPullRequest` (verified live: 200 on success, HTTP 404 when the PR is not
    in the queue). There is no reorder endpoint — a mid-queue reshuffle cannot be scripted, only
    cancel and re-submit.
    """
    payload = trunk_api.pr_payload(pr, repo)
    _, error = trunk_api.post("cancelPullRequest", payload, token)
    if error is None:
        print(
            f"PR #{pr} cancelled from the Trunk merge queue",
            file=sys.stderr,
            flush=True,
        )
        return 0
    if error == "HTTP 404":
        print(
            f"PR #{pr} is not in the Trunk merge queue — nothing to cancel",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"[ci] Trunk queue cancel error: {error}", file=sys.stderr, flush=True)
    return 4


def _trunk_queue_status(repo: str, *, token: str, as_json: bool) -> int:
    """Print the Trunk queue state and enqueued PRs; 0 ok, 3 API error.
    `--json` prints the raw `getQueue` response (queue config plus `enqueuedPullRequests`, each
    carrying state / priority / sha). States are lowercase as returned: queued / pending / testing
    / merged / failed / cancelled.
    """
    payload = trunk_api.target_payload(repo)
    data, error = trunk_api.post("getQueue", payload, token)
    if error is not None:
        print(f"[ci] Trunk queue status error: {error}", file=sys.stderr, flush=True)
        return 3
    if data is None:
        print("[ci] Trunk queue status returned no data", file=sys.stderr, flush=True)
        return 3
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    state = data.get("state", "unknown")
    print(f"Trunk merge queue: state={state} concurrency={data.get('concurrency', '?')}")
    raw_items = data.get("enqueuedPullRequests", [])
    items = raw_items if isinstance(raw_items, list) else []
    if not items:
        print("No PRs in the queue.")
        return 0
    for item in items:
        priority = item.get("priorityName") or item.get("priorityValue")
        sha = str(item.get("prSha") or "")
        print(
            f"  #{item.get('prNumber')} [{item.get('state')}] "
            f"{item.get('prTitle')} (priority {priority}, sha {sha[:8]})"
        )
    return 0


def _trunk_operator_command(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int | None:
    """Dispatch --queue-status / --evict when set; None when neither is.
    Both touch the live Trunk queue and need TRUNK_API_TOKEN (exit 3 when unset). Usage errors go
    through parser.error, which exits 2, matching the rest of the CLI's contract.
    """
    if not (args.queue_status or args.evict):
        return None
    if args.wait or args.merge or args.rerun_failed_jobs:
        parser.error("--queue-status/--evict are exclusive with --wait/--merge/--rerun-failed-jobs")
    if args.queue_status and args.evict:
        parser.error("--queue-status and --evict are mutually exclusive")
    if args.evict and args.json:
        parser.error("--json is not supported with --evict")
    if args.evict and args.pr is None:
        parser.error("PR number is required with --evict")
    trunk_token = os.environ.get("TRUNK_API_TOKEN")
    if not trunk_token:
        print(
            "TRUNK_API_TOKEN is required for Trunk queue operations",
            file=sys.stderr,
            flush=True,
        )
        return 3
    if args.evict:
        return _trunk_cancel(args.pr, args.repo, token=trunk_token)
    return _trunk_queue_status(args.repo, token=trunk_token, as_json=args.json)


def _rerun_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int | None:
    """Dispatch --rerun-failed-jobs when set; None when not set."""
    if not args.rerun_failed_jobs:
        return None
    if args.wait or args.merge or args.json:
        parser.error("--rerun-failed-jobs is exclusive with --wait/--merge/--json")
    try:
        if args.dry_run:
            jobs = list_failed_jobs(args.pr, args.repo)
            if not jobs:
                print("No failed jobs to re-run")
                return 0
            for j in jobs:
                print(
                    f"{j['name']} (job {j['job_id']}, run {j['run_id']}, "
                    f"{j['conclusion']}) [run {j['run_status']}]"
                )
            return 0
        reran, waiting, errors = rerun_failed_jobs(args.pr, args.repo)
    except CiJobRerunError as error:
        # A failed GitHub query must not read as "no failed jobs" (issue #1945).
        print(f"Failed to list failed jobs: {error}", file=sys.stderr, flush=True)
        return 1
    for j in reran:
        print(f"Re-ran {j['name']} (job {j['job_id']})")
    for w in waiting:
        print(
            f"Waiting: {w['name']} (job {w['job_id']}, run {w['run_id']}): the run "
            f"is still {w['run_status']}; GitHub refuses re-runs until it completes. "
            "Re-run this command once the run finishes."
        )
    for e in errors:
        print(f"Re-run failed: {e}")
    return 3 if errors else 5 if waiting else 0


def _watch_trunk_enqueue(
    pr: str,
    repo: str,
    *,
    every: int,
    deadline: float | None,
    timeout: int,
    token: str,
) -> int:
    """Poll Trunk's submitted-PR state until it merges, fails, or times out.
    Terminal states: "merged", "failed", "cancelled". Everything else — including "pending"
    (waiting for a batch), "not_ready" (required statuses not yet green), and "testing" (merge-tree
    test run in progress, observed live 2026-09-03) — is non-terminal: keep polling.
    """
    payload = trunk_api.pr_payload(pr, repo)
    consecutive_errors = 0
    while True:
        data, error = trunk_api.post("getSubmittedPullRequest", payload, token)
        if error is not None:
            consecutive_errors += 1
            print(
                f"[ci] Trunk queue poll error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {error}",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"PR #{pr} Trunk queue error after {MAX_CONSECUTIVE_ERRORS} attempts: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 3
        else:
            if data is None:
                print("[ci] Trunk queue poll returned no data", file=sys.stderr, flush=True)
                return 3
            consecutive_errors = 0
            state = data.get("state")
            if state == "merged":
                print(f"PR #{pr} merged by the Trunk merge queue")
                return 0
            if state in {"failed", "cancelled"}:
                reason = data.get("reason") or state
                print(
                    f"PR #{pr} Trunk queue {state}: {reason} — "
                    f"full getSubmittedPullRequest payload: {json.dumps(data, indent=2)}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(f"[ci] Trunk queue state: {state or 'unknown'}", file=sys.stderr, flush=True)
        if _deadline_hit(deadline, pr, timeout, "still in the Trunk merge queue"):
            return 1
        time.sleep(every)


def _trunk_merge_flow(
    pr: str,
    repo: str,
    priority: str,
    *,
    every: int,
    timeout: int,
    token: str,
    require_fresh_base: bool = False,
) -> int:
    """Apply the standard cooldown/green recheck, then submit and watch Trunk."""
    deadline = time.monotonic() + timeout if timeout else None
    while (remaining := _queue_cooldown_seconds(pr, repo)) > 0:
        if _deadline_hit(deadline, pr, timeout, "queue cooldown not finished"):
            return 1
        print(
            f"PR #{pr} head updated {remaining}s ago — queue cooldown, waiting",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(min(remaining, every))

    result = status.check_ci(pr, repo=repo)
    if result.verdict is CIStatus.ERROR:
        print(
            f"PR #{pr} CI error before queueing: {result.error_detail}",
            file=sys.stderr,
            flush=True,
        )
        return 3
    if result.verdict is not CIStatus.ALL_PASSED:
        print(f"PR #{pr} CI no longer green: {result.summary()}", file=sys.stderr, flush=True)
        return 1

    stale, unreadable = trunk_api.base_freshness(pr, repo)
    if stale is not None:
        base_sha, main_sha = stale
        print(
            f"PR #{pr} base {base_sha[:8]} lags main {main_sha[:8]} — main advanced since this "
            "PR last synced, so Trunk will re-test against the newer base (an extra "
            "in-queue round). Rebase onto main before submitting to skip it.",
            file=sys.stderr,
            flush=True,
        )
        if require_fresh_base:
            print(
                f"PR #{pr} not submitted: --require-fresh-base is set",
                file=sys.stderr,
                flush=True,
            )
            return 1
    elif unreadable and require_fresh_base:
        print(
            f"PR #{pr} could not verify base freshness — submitting anyway "
            "(advisory check fails open)",
            file=sys.stderr,
            flush=True,
        )

    rc = _submit_trunk(pr, repo, priority, token=token)
    if rc != 0:
        return rc
    return _watch_trunk_enqueue(
        pr, repo, every=every, deadline=deadline, timeout=timeout, token=token
    )


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run an explicitly selected owner command, validating credentials before waiting."""
    try:
        _resolve_queue(args.queue)
    except ValueError as error:
        parser.error(str(error))
    operator_rc = _trunk_operator_command(args, parser)
    if operator_rc is not None:
        return operator_rc
    if args.pr is None:
        parser.error("PR number is required")
    if args.rerun_failed_jobs:
        rerun_rc = _rerun_command(args, parser)
        if rerun_rc is None:
            raise AssertionError("selected re-run command returned no result")
        return rerun_rc
    if args.merge:
        args.wait = True
        if args.timeout == 0:
            args.timeout = 1800
        token = os.environ.get("TRUNK_API_TOKEN")
        if not token:
            print(
                "TRUNK_API_TOKEN is required for --queue trunk --merge", file=sys.stderr, flush=True
            )
            return 3
        from scripts.ci.monitor import wait_for_verdict

        rc = wait_for_verdict(args.pr, args.repo, args.every, args.timeout)
        if rc != 0:
            return rc
        return _trunk_merge_flow(
            args.pr,
            args.repo,
            args.priority,
            every=args.every,
            timeout=args.timeout,
            token=token,
            require_fresh_base=args.require_fresh_base,
        )
    raise AssertionError("owner dispatch requires an explicit operation")
