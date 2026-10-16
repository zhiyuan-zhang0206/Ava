#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from base.host.system.hostport import Host

_GH_TIMEOUT_S = 60.0
LINUX_MINUTE_USD = 0.006
MACOS_MINUTE_USD = 0.062
INCLUDED_MINUTES_MONTHLY = 3000

DEFAULT_REPO = "zhiyuan-zhang0206/Ava"
_LEDGER_DIR = Path(__file__).resolve().parents[1] / "ci_usage"
DEFAULT_LEDGER = _LEDGER_DIR / "ledger.jsonl"

_AGENT_RE = re.compile(r"\[Ava-(\d+)\]")
_TASK_RE = re.compile(r"\(task #(\d+)\)")
_TRUNK_PR_RE = re.compile(r"trunk-merge/pr-(\d+)/")
_BRANCH_AGENT_RE = re.compile(r"^ava-(\d+)-")

_WINDOW_CHUNK_SECONDS = 4 * 3600


def _gh_api(path: str, *, jq: str, host: Host) -> list[dict]:
    try:
        result = host.run(
            ["gh", "api", path, "--jq", jq],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_identity(text: str) -> tuple[int | None, int | None]:
    agent_match = _AGENT_RE.search(text)
    task_match = _TASK_RE.search(text)
    agent_id = int(agent_match.group(1)) if agent_match else None
    task_id = int(task_match.group(1)) if task_match else None
    return agent_id, task_id


def _pr_title(repo: str, pr_number: int | None, *, host: Host) -> str:
    if pr_number is None:
        return ""
    try:
        result = host.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "title",
                "--jq",
                ".title",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _real_pr_of_trunk_branch(head_ref: str) -> int | None:
    match = _TRUNK_PR_RE.search(head_ref or "")
    return int(str(match.group(1))) if match else None


# ava:ref scripts/ci/pull_requests/docs/accounting.ava.okf.md#minute-billing
def job_minutes(started: str | None, completed: str | None) -> int:
    if not started or not completed:
        return 0
    start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    return max(0, math.ceil((end - start).total_seconds() / 60.0))


def _identity_from_prs(
    run: dict, repo: str, real_pr: int | None, *, host: Host
) -> tuple[int | None, int | None, int | None]:
    pull_requests = [p for p in run.get("pull_requests", []) if isinstance(p, dict)]
    if pull_requests:
        raw_number = pull_requests[0].get("number")
        pr_number = int(raw_number) if isinstance(raw_number, (int, str)) else None
        title = _pr_title(repo, real_pr if real_pr else pr_number, host=host)
        agent_id, task_id = parse_identity(title)
        return (real_pr or pr_number), agent_id, task_id
    if real_pr is not None:
        agent_id, task_id = parse_identity(_pr_title(repo, real_pr, host=host))
        return real_pr, agent_id, task_id
    return None, None, None


def _merged_pr_of(run: dict, repo: str, *, host: Host) -> int | None:
    head_sha = str(run.get("head_sha") or "")
    if not head_sha:
        return None
    merged_prs = _gh_api(
        f"repos/{repo}/commits/{head_sha}/pulls", jq="[.[] | .number] | @json", host=host
    )
    return merged_prs[0] if merged_prs and isinstance(merged_prs[0], int) else None


# ava:ref scripts/ci/pull_requests/docs/accounting.ava.okf.md#attribution-fallback-chain
def _run_identity(run: dict, repo: str, *, host: Host) -> tuple[int | None, int | None, int | None]:
    real_pr = _real_pr_of_trunk_branch(str(run.get("head_branch") or ""))
    pr_number, agent_id, task_id = _identity_from_prs(run, repo, real_pr, host=host)
    if agent_id is None and str(run.get("head_branch") or "") == "main":
        merged = _merged_pr_of(run, repo, host=host)
        if merged is not None:
            pr_number = merged
            agent_id, task_id = parse_identity(_pr_title(repo, pr_number, host=host))
    if agent_id is None:
        agent_id, task_id = parse_identity(str(run.get("head_commit_msg") or ""))
    if agent_id is None:
        branch_match = _BRANCH_AGENT_RE.match(str(run.get("head_branch") or ""))
        if branch_match:
            agent_id = int(str(branch_match.group(1)))
    return pr_number, agent_id, task_id


def _run_entry(run: dict, repo: str, *, host: Host) -> dict:
    pr_number, agent_id, task_id = _run_identity(run, repo, host=host)
    day = (run.get("created_at") or "")[:10]
    return {
        "run_id": int(run["id"]),
        "day": day,
        "agent_id": agent_id,
        "task_id": task_id,
        "pr_number": pr_number,
        "name": run.get("name"),
        "head_branch": run.get("head_branch"),
        "conclusion": run.get("conclusion"),
        "linux_minutes": 0,
        "macos_minutes": 0,
        "jobs": 0,
    }


def _fill_job_minutes(entry: dict, repo: str, *, host: Host) -> None:
    jobs = _gh_api(
        f"repos/{repo}/actions/runs/{entry['run_id']}/jobs?per_page=100",
        jq="[.jobs[] | {name: .name, started: .started_at, completed: .completed_at}] | @json",
        host=host,
    )
    for job in jobs:
        started = job.get("started")
        completed = job.get("completed")
        minutes = job_minutes(
            started if isinstance(started, str) else None,
            completed if isinstance(completed, str) else None,
        )
        name = str(job.get("name") or "")
        if "macos" in name.lower():
            entry["macos_minutes"] += minutes
        else:
            entry["linux_minutes"] += minutes
        entry["jobs"] += 1


def collect(repo: str, since: str, until: str, *, host: Host) -> list[dict[str, Any]]:
    entries: dict[int, dict] = {}
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    cursor = since_dt
    while cursor < until_dt:
        chunk_end = min(cursor.timestamp() + _WINDOW_CHUNK_SECONDS, until_dt.timestamp())
        window = (
            datetime.fromtimestamp(cursor.timestamp(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            + ".."
            + datetime.fromtimestamp(chunk_end, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        for page in range(1, 11):
            runs = _gh_api(
                f"repos/{repo}/actions/runs?per_page=100&page={page}&created={window}",
                jq='[.workflow_runs[] | select(.name == "CI") | '
                "{id: .id, name: .name, conclusion: .conclusion, created_at: .created_at, "
                "head_branch: .head_branch, head_sha: .head_sha, "
                "head_commit_msg: .head_commit.message, "
                "pull_requests: [.pull_requests[]?.number]}] | @json",
                host=host,
            )
            if not runs:
                break
            for run in runs:
                entries[int(run["id"])] = run
            if len(runs) < 100:
                break
        cursor = datetime.fromtimestamp(chunk_end, tz=UTC)
    out = []
    for run in entries.values():
        entry = _run_entry(run, repo, host=host)
        _fill_job_minutes(entry, repo, host=host)
        out.append(entry)
    return out


def load_ledger(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    ledger: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("run_id"), int):
            ledger[entry["run_id"]] = entry
    return ledger


# ava:ref scripts/ci/pull_requests/docs/accounting.ava.okf.md#ledger-idempotence
def append_ledger(path: Path, entries: list[dict]) -> int:
    ledger = load_ledger(path)
    fresh = [e for e in entries if e["run_id"] not in ledger]
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in sorted(fresh, key=lambda e: e["run_id"]):
            fh.write(json.dumps(entry) + "\n")
    return len(fresh)


# ava:ref scripts/ci/pull_requests/docs/accounting.ava.okf.md#overage-equivalent-cost
def report_rows(ledger: dict[int, dict], *, days: int, agent: int | None) -> list[dict]:
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    per_agent: dict[int | None, dict] = {}
    for entry in ledger.values():
        if agent is not None and entry.get("agent_id") != agent:
            continue
        try:
            created = datetime.fromisoformat(
                str(entry.get("day") or "") + "T00:00:00+00:00"
            ).timestamp()
        except ValueError:
            created = 0.0
        if created < cutoff:
            continue
        key = entry.get("agent_id") if isinstance(entry.get("agent_id"), int) else None
        if key not in per_agent:
            per_agent[key] = {
                "agent_id": key,
                "runs": 0,
                "linux_minutes": 0,
                "macos_minutes": 0,
            }
        row = per_agent[key]
        row["runs"] += 1
        row["linux_minutes"] += int(entry.get("linux_minutes") or 0)
        row["macos_minutes"] += int(entry.get("macos_minutes") or 0)
    rows = list(per_agent.values())
    for row in rows:
        row["est_usd"] = round(
            row["linux_minutes"] * LINUX_MINUTE_USD + row["macos_minutes"] * MACOS_MINUTE_USD,
            2,
        )
    return sorted(rows, key=lambda r: -(r["est_usd"] or 0))


def _print_report(rows: list[dict], days: int) -> None:
    print(
        f"CI usage, last {days} days (minutes primary; est. private-repo overage at "
        f"Linux ${LINUX_MINUTE_USD}/min, macOS ${MACOS_MINUTE_USD}/min):"
    )
    if not rows:
        print("No attributed runs.")
        return
    for row in rows:
        agent = row["agent_id"] if row["agent_id"] is not None else "unattributed"
        print(
            f"  #{agent}: {row['runs']} runs, {row['linux_minutes']} linux + "
            f"{row['macos_minutes']} macos minutes, est ${row['est_usd']}"
        )


def _report(days: int, agent: int | None, *, as_json: bool) -> None:
    rows = report_rows(load_ledger(DEFAULT_LEDGER), days=days, agent=agent)
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        _print_report(rows, days)


def _print_entries(entries: list[dict]) -> None:
    if not entries:
        print("No CI runs in the window.")
        return
    for entry in entries:
        agent = entry["agent_id"] if entry["agent_id"] is not None else "unattributed"
        print(
            f"run {entry['run_id']} day={entry['day']} #{agent} "
            f"(task {entry['task_id'] if entry['task_id'] is not None else '-'}) "
            f"pr={entry['pr_number'] if entry['pr_number'] is not None else '-'} "
            f"{entry['linux_minutes']}L+{entry['macos_minutes']}M min "
            f"[{entry['conclusion']}]"
        )


def main(argv: list[str] | None = None, *, host: Host) -> int:
    parser = argparse.ArgumentParser(description="CI usage attribution and ledger")
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"owner/repo (default: {DEFAULT_REPO})"
    )
    parser.add_argument("--since", help="ISO start (e.g. 2026-09-05T16:00Z)")
    parser.add_argument("--until", help="ISO end (exclusive)")
    parser.add_argument("--append-ledger", help="append entries to this JSONL ledger idempotently")
    parser.add_argument("--report", action="store_true", help="per-agent rollup from a ledger")
    parser.add_argument("--days", type=int, default=7, help="with --report: window in days")
    parser.add_argument(
        "--agent", type=int, default=None, help="with --report: restrict to one agent"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.report:
        if args.since or args.until or args.append_ledger:
            parser.error("--report is exclusive with --since/--until/--append-ledger")
        _report(args.days, args.agent, as_json=args.json)
        return 0

    if not args.since or not args.until:
        parser.error("--since and --until are required without --report")
    entries = collect(args.repo, args.since, args.until, host=host)
    if args.append_ledger:
        appended = append_ledger(Path(args.append_ledger), entries)
        print(f"appended {appended} entry(s) to {args.append_ledger} (of {len(entries)} collected)")
        return 0
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    _print_entries(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main(host=Host.system()))
