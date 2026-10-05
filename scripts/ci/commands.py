#!/usr/bin/env python3
"""Query or wait for genuine GitHub CI evidence; optional owner operations use the same CLI.

`check_ci`, `CIResult` and `CIStatus` are stable reusable imports. Pending, missing,
draft-skipped or unexecuted workflow evidence is never green. Limbo diagnostics
name aged queued runs without changing that verdict.

Usage:
    .venv/bin/python scripts/ci_utils.py PR [--repo owner/repo] [--json]
    .venv/bin/python scripts/ci_utils.py PR --wait [--timeout N]
    .venv/bin/python scripts/ci_utils.py PR --diagnose [--json]
    .venv/bin/python scripts/ci_utils.py PR --merge [--queue trunk]

One-shot queries retain their reporting contract: pending/errors print a structured
verdict and exit 0; settled failures exit 1. Waiting exits 0 only for ALL_PASSED,
1 for not-green or timeout, and 3 for persistent query errors. Explicit owner
operations retain queue authentication, cooldown, priority and job re-run behavior.
No status override is supported.
"""

from __future__ import annotations

import argparse
import json
import os

from scripts.ci import monitor
from scripts.ci.accounting import DEFAULT_LEDGER, load_ledger, report_rows
from scripts.ci.status import DEFAULT_REPO, POLL_INTERVAL

# CLI metadata does not import the queue operations module.
_QUEUE_CHOICES = ("trunk",)
_TRUNK_PRIORITIES = ("urgent", "high", "medium", "low")


def _validate_common_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Shared usage validation, kept out of main()'s statement budget."""
    if args.every <= 0:
        parser.error("--every must be positive")
    if args.timeout < 0:
        parser.error("--timeout must be >= 0")
    if args.json and args.wait:
        parser.error("--json and --wait are mutually exclusive")


def _ci_usage_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int | None:
    """Dispatch --ci-usage when set; None when not set.
    Reads the repo ledger (scripts/ci/ci_usage/ledger.jsonl, produced by scripts/ci/accounting.py
    --append-ledger) and prints per-agent rollups — the read side of the CI cost attribution
    pipeline (task #2575).
    """
    if not args.ci_usage:
        return None
    if (
        args.wait
        or args.merge
        or args.rerun_failed_jobs
        or args.queue_status
        or args.evict
        or args.diagnose
    ):
        parser.error(
            "--ci-usage is exclusive with --wait/--merge/--rerun-failed-jobs/"
            "--queue-status/--evict/--diagnose"
        )
    if args.pr is not None:
        parser.error("--ci-usage takes no PR number")
    rows = report_rows(
        load_ledger(DEFAULT_LEDGER), days=args.ci_usage_days, agent=args.ci_usage_agent
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No attributed CI runs in the ledger window.")
        return 0
    for row in rows:
        agent = row["agent_id"] if row["agent_id"] is not None else "unattributed"
        print(
            f"  #{agent}: {row['runs']} runs, {row['linux_minutes']} linux + "
            f"{row['macos_minutes']} macos minutes, est ${row['est_usd']}"
        )
    return 0


def _diagnose_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int | None:
    """Dispatch --diagnose when set; None when not set."""
    if not args.diagnose:
        return None
    if args.wait or args.merge or args.rerun_failed_jobs or args.queue_status or args.evict:
        parser.error(
            "--diagnose is exclusive with --wait/--merge/--rerun-failed-jobs/--queue-status/--evict"
        )
    if args.pr is None:
        parser.error("PR number is required with --diagnose")
    from scripts.ci.ci_diagnose import diagnose_pr, print_diagnosis

    diag = diagnose_pr(args.pr, args.repo, token=os.environ.get("TRUNK_API_TOKEN"))
    if args.json:
        print(json.dumps(diag, indent=2))
    else:
        print_diagnosis(diag)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Default: one-shot query (legacy behavior). `--wait`: poll
    until the verdict settles with the monitor exit-code contract; `--merge`
    implies `--wait` and submits to Trunk once green."""
    p = argparse.ArgumentParser(description="Check CI status of a GitHub PR")
    p.add_argument("pr", nargs="?", help="PR number (required except --queue-status)")
    p.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"owner/repo (default: {DEFAULT_REPO})",
    )
    p.add_argument("--json", action="store_true", help="Output full JSON (one-shot only)")
    p.add_argument(
        "--wait",
        action="store_true",
        help="poll until the verdict settles instead of querying once",
    )
    p.add_argument(
        "--every",
        type=int,
        default=POLL_INTERVAL,
        help=f"with --wait: poll interval in seconds (default: {POLL_INTERVAL})",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="with --wait: stop after N seconds still pending (0 = forever)",
    )
    p.add_argument(
        "--merge",
        action="store_true",
        help="with --wait: enqueue the PR once CI is green and wait for the selected "
        "merge queue to land it, with head-update cooldown (implies --wait; "
        "default timeout 1800s)",
    )
    p.add_argument(
        "--queue",
        choices=_QUEUE_CHOICES,
        default=None,
        help="with --merge: merge queue (default: CI_QUEUE, else trunk)",
    )
    p.add_argument(
        "--priority",
        choices=tuple(_TRUNK_PRIORITIES),
        default="medium",
        help="with --queue trunk: queue priority (default: medium)",
    )
    p.add_argument(
        "--require-fresh-base",
        action="store_true",
        help="with --merge: refuse to submit when main has advanced past the PR's "
        "recorded base (the queue would re-test against the newer base)",
    )
    p.add_argument(
        "--ci-usage",
        action="store_true",
        help="per-agent CI minute rollup from the repo ledger "
        "(scripts/ci/ci_usage/ledger.jsonl); with --days N (default 7) and "
        "optional --ci-usage-agent ID. --json for machine-readable output.",
    )
    p.add_argument(
        "--ci-usage-agent",
        type=int,
        default=None,
        help="with --ci-usage: restrict the rollup to one agent id",
    )
    p.add_argument(
        "--ci-usage-days",
        type=int,
        default=7,
        help="with --ci-usage: rollup window in days (default 7)",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="diagnose why the PR's CI / queue attempt failed: reads the check "
        "rollup + job log tails, Trunk queue state, and the synthetic "
        "trunk-merge test PRs, then prints classifications with suggested actions "
        "(--json for the machine-readable report). Diagnosis only — no repair "
        "action is executed.",
    )
    p.add_argument(
        "--evict",
        action="store_true",
        help="cancel the PR from the Trunk merge queue (requires TRUNK_API_TOKEN; "
        "exit 0 cancelled, 1 not in queue, 4 queue error). Trunk's API has no "
        "reorder operation, so a mid-queue reshuffle is cancel + re-submit.",
    )
    p.add_argument(
        "--queue-status",
        action="store_true",
        help="show the Trunk merge queue state and enqueued PRs (requires "
        "TRUNK_API_TOKEN); with --json, print the raw getQueue response",
    )
    p.add_argument(
        "--rerun-failed-jobs",
        action="store_true",
        help="re-run the failed jobs of the PR's workflow runs once their run "
        "is completed (issue #102): GitHub refuses job-level and run-level "
        "reruns alike while a run is still going (probed 2026-09-17; the docs "
        "state no such precondition), so a still-running run is reported "
        "as waiting with the recovery action. Exit 0 when all failed "
        "jobs were re-run (or none were), 5 when failures remain whose run is "
        "still going, 3 on errors. Exclusive with --wait/--merge/--json.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="with --rerun-failed-jobs: list failed jobs without re-running them",
    )
    args = p.parse_args(argv)
    _validate_common_args(args, p)

    ci_usage_rc = _ci_usage_command(args, p)
    if ci_usage_rc is not None:
        return ci_usage_rc

    diagnose_rc = _diagnose_command(args, p)
    if diagnose_rc is not None:
        return diagnose_rc

    if args.merge or args.queue_status or args.evict or args.rerun_failed_jobs:
        from scripts.ci.owner_operations import dispatch

        return dispatch(args, p)
    if args.pr is None:
        p.error("PR number is required")
    if args.wait:
        return monitor.wait_for_verdict(args.pr, args.repo, args.every, args.timeout)
    return monitor.query_once(args.pr, args.repo, as_json=args.json)
