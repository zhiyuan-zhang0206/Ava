"""Export GitHub Actions run observability as re-emitted OTLP gauges.

The daily sampler reads GitHub Actions and closed pull requests with ``gh api``
GETs only. It recomputes the last 30 complete cluster-time days, so each
absolute aggregate remains visible through Prometheus's 15-day retention even
when the original day's process has exited. ``ci_runs_daily`` carries day
aggregates, ``ci_workflow_window`` ranks workflows over the trailing window,
and ``ci_runs_run`` is the API-budget breadcrumb.

Persistence lives in ``$AVA_HOME/state/ci-runs/``, not the source checkout:
one cache per repository survives source resets and lets a normal daily run
re-read only the last 48 hours. A cold walk is bounded at ten 100-run pages per
created-at cursor and normally costs hundreds of GETs; a warm run costs tens.
The closed-PR list is authoritative for attribution, therefore any persistent
GitHub failure aborts before cache writes or emission so old gauges remain.

The labels below intentionally overlap: a failed retry can also be abandoned,
for example. White-run share deduplicates only its three named classes, with
instant skip taking precedence over superseded over abandoned. v1 does not
aggregate failure signatures or job timing: those remain phase-two follow-ups.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_REPO = "zhiyuan-zhang0206/Ava"
# Thirty days matches PR flow and lets the next sampler republish a day after
# Prometheus's shorter retention horizon would otherwise have expired it.
DEFAULT_WINDOW_DAYS = 30
PROCESS_NAME = "ci-runs-export"

_STATE_DIR_RELATIVE = Path("state") / "ci-runs"
_GH_TIMEOUT_SECONDS = 60.0
_PAGE_SIZE = 100  # GitHub's documented list maximum.
# Ten pages bounds each created-at cursor segment; the next cursor continues
# older data instead of silently shrinking a busy repository's observation.
_MAX_RUN_PAGES_PER_CURSOR = 10
# Closed PRs are smaller but the larger cap makes an unexpectedly busy window
# fail loudly instead of emitting an incomplete authoritative population.
_MAX_CLOSED_PR_PAGES = 80
# A run can still receive a conclusion/attempt update after creation; replay
# two days on every warm pass so those late changes replace the cached record.
_INCREMENTAL_OVERLAP = timedelta(hours=48)
# Keep a small pre-window margin because a PR can close shortly after a run
# created near the window boundary; it also makes repeated daily pruning safe.
_CACHE_MARGIN = timedelta(days=2)
_MAX_GH_ATTEMPTS = 4  # transient 429/5xx retries: bounded rate-limit recovery.

_ZOMBIE_SECONDS = 6 * 60 * 60  # GitHub wall time after runner death is an artifact.
_INSTANT_SKIP_SECONDS = 2  # zero-execution proof/watchdog wrapper signature.
_SUPERSEDE_SECONDS = 300  # replacement run must follow the cancellation promptly.
_TRUNK_MERGE_BRANCH = re.compile(r"^trunk-merge/pr-(\d+)(?:/|$)")
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
# Rerun-recovery/machinery wrappers: Ava's CI failed-job rerun and Monsora's
# Runner shutdown rerun must remain visible as separately filterable noise.
_WATCHDOG_WORKFLOW_NAMES = frozenset({"CI failed-job rerun", "Runner shutdown rerun"})


class CiRunsError(RuntimeError):
    """An authoritative GitHub response made this sampling pass unusable."""


@dataclass
class FetchStats:
    """The per-sampler GitHub request accounting carried by the breadcrumb."""

    api_requests: int = 0
    retries: int = 0


@dataclass
class RepoCollection:
    """One repository's computed state, retained until every repo succeeds."""

    repo: str
    cache: dict[str, Any]
    daily: dict[str, dict[str, Any]]
    workflows: dict[str, dict[str, Any]]
    window_runs: int
    window_prs: int
    api_requests: int


def cluster_tz() -> ZoneInfo:
    """Return the configured cluster clock used for every date label."""
    from shared.config import settings

    return ZoneInfo(settings.general.timezone)


def complete_days(now: datetime, days: int, tz: ZoneInfo) -> list[date]:
    """Return the requested complete cluster-time days, oldest first."""
    last_complete = now.astimezone(tz).date() - timedelta(days=1)
    return [last_complete - timedelta(days=index) for index in range(days - 1, -1, -1)]


def parse_timestamp(value: object) -> datetime | None:
    """Parse a GitHub ISO timestamp as UTC; malformed optional values are absent."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def wall_seconds(run: dict[str, Any]) -> float | None:
    """Return observed wall time from start (or creation) to latest update."""
    started = parse_timestamp(run.get("run_started_at")) or parse_timestamp(run.get("created_at"))
    updated = parse_timestamp(run.get("updated_at"))
    if started is None or updated is None:
        return None
    return max(0.0, (updated - started).total_seconds())


def is_zombie(run: dict[str, Any]) -> bool:
    """A six-hour wall clock is a runner-death artifact, not execution time."""
    wall = wall_seconds(run)
    return wall is not None and wall > _ZOMBIE_SECONDS


def is_instant_skip(run: dict[str, Any]) -> bool:
    """Skipped wrappers that executed no useful work."""
    wall = wall_seconds(run)
    return run.get("conclusion") == "skipped" and wall is not None and wall <= _INSTANT_SKIP_SECONDS


def is_failed(run: dict[str, Any]) -> bool:
    """Whether GitHub gave this run one of the CI-red conclusions."""
    return run.get("conclusion") in _FAILED_CONCLUSIONS


def workflow_class(name: object) -> str:
    """Map the conservative reviewed workflow-name rules to one noise class."""
    label = str(name or "")
    lowered = label.lower()
    if label in _WATCHDOG_WORKFLOW_NAMES:
        return "watchdog"
    if label.startswith("QA "):
        return "qa_gate"
    if "proof" in lowered or "evidence" in lowered or lowered.endswith(("contracts", "contract")):
        return "proof"
    if label.startswith("Release"):
        return "release"
    return "core"


def superseded_ids(runs: Iterable[dict[str, Any]]) -> set[int]:
    """Cancelled runs promptly replaced on the same workflow/branch pair."""
    grouped: dict[tuple[object, object], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[(run.get("workflow_id"), run.get("head_branch"))].append(run)
    result: set[int] = set()
    for siblings in grouped.values():
        siblings.sort(
            key=lambda run: (
                parse_timestamp(run.get("created_at")) or datetime.min.replace(tzinfo=UTC)
            )
        )
        for index, run in enumerate(siblings):
            if run.get("conclusion") != "cancelled":
                continue
            cancelled_created = parse_timestamp(run.get("created_at"))
            updated = parse_timestamp(run.get("updated_at"))
            if cancelled_created is None or updated is None:
                continue
            for newer in siblings[index + 1 :]:
                created = parse_timestamp(newer.get("created_at"))
                if created is None or created <= cancelled_created:
                    continue
                delay = (created - updated).total_seconds()
                if delay > _SUPERSEDE_SECONDS:
                    break
                if abs(delay) <= _SUPERSEDE_SECONDS:
                    result.add(int(run["id"]))
                    break
    return result


def trunk_merge_pr_number(branch: object) -> int | None:
    """Resolve Trunk's synthetic branch to its real PR number."""
    match = _TRUNK_MERGE_BRANCH.match(str(branch or ""))
    return int(match[1]) if match else None


def _run_gh(args: list[str], stats: FetchStats) -> str:
    """Issue one read-only gh command with bounded transient-error retries."""
    for attempt in range(_MAX_GH_ATTEMPTS):
        try:
            result = subprocess.run(  # noqa: S603 - fixed gh argv, no shell
                ["gh", "api", "--method", "GET", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            detail = str(exc)
            retryable = True
        else:
            stats.api_requests += 1
            if result.returncode == 0:
                return result.stdout
            detail = (result.stderr or result.stdout or "").strip()
            retryable = any(code in detail for code in ("429", "500", "502", "503", "504"))
        if not retryable or attempt == _MAX_GH_ATTEMPTS - 1:
            raise CiRunsError(f"gh api {' '.join(args[:2])} failed: {detail[-500:]}")
        stats.retries += 1
        time.sleep(2 * (attempt + 1))
    raise AssertionError("bounded gh retry loop exhausted without result")


def _json_list(output: str, description: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CiRunsError(f"invalid JSON from {description}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise CiRunsError(f"unexpected {description} payload")
    return parsed


def fetch_runs(
    repo: str, since: datetime, until: datetime, stats: FetchStats
) -> list[dict[str, Any]]:
    """Walk runs newest-first using created<= cursors and id deduplication."""
    cursor = until
    seen: dict[int, dict[str, Any]] = {}
    jq = (
        "[.workflow_runs[] | {id, workflow_id, name, event, status, conclusion, run_attempt, "
        "head_branch, head_sha, created_at, run_started_at, updated_at}] | @json"
    )
    while cursor >= since:
        oldest: datetime | None = None
        for page in range(1, _MAX_RUN_PAGES_PER_CURSOR + 1):
            stamp = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
            path = (
                f"repos/{repo}/actions/runs?created=%3C%3D{stamp}&per_page={_PAGE_SIZE}&page={page}"
            )
            batch = _json_list(_run_gh([path, "--jq", jq], stats), "workflow runs")
            if not batch:
                return list(seen.values())
            for run in batch:
                identifier = run.get("id")
                created = parse_timestamp(run.get("created_at"))
                if not isinstance(identifier, int) or created is None:
                    raise CiRunsError("workflow run lacks an integer id or created_at")
                seen[identifier] = run
                oldest = created if oldest is None or created < oldest else oldest
            if len(batch) < _PAGE_SIZE:
                return list(seen.values())
        if oldest is None or oldest < since:
            return list(seen.values())
        next_cursor = oldest - timedelta(seconds=1)
        if next_cursor >= cursor:
            raise CiRunsError("workflow-runs cursor did not move backward")
        cursor = next_cursor
    return list(seen.values())


def fetch_closed_prs(repo: str, since: datetime, stats: FetchStats) -> list[dict[str, Any]]:
    """Walk the authoritative closed-PR population newest-updated first."""
    jq = (
        "[.[] | {number, head_ref: .head.ref, head_sha: .head.sha, created_at, updated_at, "
        "merged_at, closed_at}] | @json"
    )
    records: list[dict[str, Any]] = []
    for page in range(1, _MAX_CLOSED_PR_PAGES + 1):
        path = f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={_PAGE_SIZE}&page={page}"
        batch = _json_list(_run_gh([path, "--jq", jq], stats), "closed pull requests")
        records.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return records
        stamps = [parse_timestamp(entry.get("updated_at")) for entry in batch]
        oldest = min((stamp for stamp in stamps if stamp is not None), default=None)
        if oldest is not None and oldest < since:
            return records
    raise CiRunsError(f"closed-PR walk exceeded {_MAX_CLOSED_PR_PAGES} pages")


def load_cache(path: Path) -> dict[str, Any]:
    """Load one repository cache; absent or corrupt cache heals via a cold walk."""
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"runs": {}, "prs": {}, "last_fetch_end": None}
    if not isinstance(cached, dict):
        return {"runs": {}, "prs": {}, "last_fetch_end": None}
    return {
        "runs": cached["runs"] if isinstance(cached.get("runs"), dict) else {},
        "prs": cached["prs"] if isinstance(cached.get("prs"), dict) else {},
        "last_fetch_end": cached.get("last_fetch_end"),
    }


def _completed_at(pr: dict[str, Any]) -> datetime | None:
    return parse_timestamp(pr.get("merged_at")) or parse_timestamp(pr.get("closed_at"))


def merge_cache(
    cache: dict[str, Any],
    fresh_runs: Iterable[dict[str, Any]],
    fresh_prs: Iterable[dict[str, Any]],
    *,
    cache_start: datetime,
    fetched_end: datetime,
) -> dict[str, Any]:
    """Merge updated API records by stable id and prune only old cache state."""
    runs = {str(key): value for key, value in cache["runs"].items() if isinstance(value, dict)}
    prs = {str(key): value for key, value in cache["prs"].items() if isinstance(value, dict)}
    for run in fresh_runs:
        runs[str(run["id"])] = run
    for pr in fresh_prs:
        prs[str(pr["number"])] = pr
    runs = {
        key: run
        for key, run in runs.items()
        if (created := parse_timestamp(run.get("created_at"))) is not None
        and created >= cache_start
    }
    prs = {
        key: pr
        for key, pr in prs.items()
        if (completed := _completed_at(pr)) is not None and completed >= cache_start
    }
    return {
        "version": 1,
        "last_fetch_end": fetched_end.astimezone(UTC).isoformat(),
        "runs": runs,
        "prs": prs,
    }


def _cache_path(state_dir: Path, repo: str) -> Path:
    """A readable repo-derived cache filename without path separators."""
    return state_dir / f"{repo.replace('/', '--')}.json"


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist cache/snapshot state so crashes preserve the prior file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _runs_in_days(
    runs: Iterable[dict[str, Any]], days: set[date], tz: ZoneInfo
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in runs:
        created = parse_timestamp(run.get("created_at"))
        if created is not None and created.astimezone(tz).date() in days:
            result.append(run)
    return result


def attribute_runs(runs: Iterable[dict[str, Any]], prs: Iterable[dict[str, Any]]) -> dict[int, int]:
    """Return one best closed-PR attribution per run id, based on branch life."""
    by_number: dict[int, dict[str, Any]] = {}
    by_branch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pr in prs:
        number = pr.get("number")
        branch = pr.get("head_ref")
        if not isinstance(number, int) or not isinstance(branch, str) or _completed_at(pr) is None:
            continue
        by_number[number] = pr
        by_branch[branch].append(pr)
    attributed: dict[int, int] = {}
    for run in runs:
        identifier = run.get("id")
        created = parse_timestamp(run.get("created_at"))
        if not isinstance(identifier, int) or created is None:
            continue
        candidates: list[dict[str, Any]] = []
        synthetic = trunk_merge_pr_number(run.get("head_branch"))
        if synthetic is not None and synthetic in by_number:
            candidates = [by_number[synthetic]]
        elif isinstance(run.get("head_branch"), str):
            candidates = by_branch[run["head_branch"]]
        valid = []
        for pr in candidates:
            opened = parse_timestamp(pr.get("created_at"))
            completed = _completed_at(pr)
            if opened is None or completed is None:
                continue
            if opened - timedelta(hours=2) <= created <= completed + timedelta(hours=1):
                valid.append(pr)
        if valid:
            selected = max(
                valid,
                key=lambda pr: (
                    parse_timestamp(pr.get("created_at")) or datetime.min.replace(tzinfo=UTC)
                ),
            )
            attributed[identifier] = int(selected["number"])
    return attributed


def percentile(values: Iterable[float], quantile: float) -> float | None:
    """Linear interpolation between closest ranks, matching PR-flow percentiles."""
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 1)


def _flags(run: dict[str, Any], superseded: set[int], abandoned_shas: set[str]) -> dict[str, bool]:
    identifier = int(run["id"])
    label = workflow_class(run.get("name"))
    wall = wall_seconds(run)
    return {
        "zombie": is_zombie(run),
        "instant_skip": is_instant_skip(run),
        "superseded": identifier in superseded,
        "superseded_zero": identifier in superseded
        and wall is not None
        and wall <= _INSTANT_SKIP_SECONDS,
        "failed": is_failed(run),
        "retried_failed": is_failed(run) and int(run.get("run_attempt") or 0) >= 2,
        "self_healed": int(run.get("run_attempt") or 0) >= 2 and run.get("conclusion") == "success",
        "abandoned": isinstance(run.get("head_sha"), str) and run["head_sha"] in abandoned_shas,
        "watchdog": label == "watchdog",
        "qa_gate": label == "qa_gate",
        "proof": label == "proof",
    }


def daily_aggregates(
    runs: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    days: list[date],
    tz: ZoneInfo,
) -> dict[str, dict[str, Any]]:
    """Compute day-labelled count, share, and per-completed-PR absolute state."""
    by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in days}
    wanted = set(days)
    for run in runs:
        created = parse_timestamp(run.get("created_at"))
        if created is not None and (stamped := created.astimezone(tz).date()) in wanted:
            by_day[stamped].append(run)
    abandoned_shas = {
        str(pr["head_sha"])
        for pr in prs
        if pr.get("merged_at") is None and isinstance(pr.get("head_sha"), str)
    }
    superseded = superseded_ids(runs)
    run_pr = attribute_runs(runs, prs)
    runs_by_id = {int(run["id"]): run for run in runs}
    prs_by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in days}
    for pr in prs:
        completed = _completed_at(pr)
        if completed is not None and (stamped := completed.astimezone(tz).date()) in wanted:
            prs_by_day[stamped].append(pr)
    result: dict[str, dict[str, Any]] = {}
    for day in days:
        group = by_day[day]
        flags = {int(run["id"]): _flags(run, superseded, abandoned_shas) for run in group}
        entry: dict[str, Any] = {
            "runs": len(group),
            "instant_skip_runs": sum(value["instant_skip"] for value in flags.values()),
            "watchdog_runs": sum(value["watchdog"] for value in flags.values()),
            "qa_gate_runs": sum(value["qa_gate"] for value in flags.values()),
            "proof_runs": sum(value["proof"] for value in flags.values()),
            "cancelled_runs": sum(run.get("conclusion") == "cancelled" for run in group),
            "superseded_runs": sum(value["superseded"] for value in flags.values()),
            "superseded_zero_runs": sum(value["superseded_zero"] for value in flags.values()),
            "failed_runs": sum(value["failed"] for value in flags.values()),
            "retried_failed_runs": sum(value["retried_failed"] for value in flags.values()),
            "self_healed_runs": sum(value["self_healed"] for value in flags.values()),
            "abandoned_runs": sum(value["abandoned"] for value in flags.values()),
            "zombie_runs": sum(value["zombie"] for value in flags.values()),
            "prs_completed": len(prs_by_day[day]),
        }
        if group:
            white = sum(
                value["instant_skip"]
                or (not value["instant_skip"] and value["superseded"])
                or (not value["instant_skip"] and not value["superseded"] and value["abandoned"])
                for value in flags.values()
            )
            entry["white_run_share"] = round(white / len(group), 3)
            entry["retry_share"] = round(
                sum(int(run.get("run_attempt") or 0) >= 2 for run in group) / len(group), 3
            )
            entry["noise_run_share"] = round(
                sum(
                    value["watchdog"] or value["qa_gate"] or value["proof"]
                    for value in flags.values()
                )
                / len(group),
                3,
            )
        completed_numbers = {
            int(pr["number"]) for pr in prs_by_day[day] if isinstance(pr.get("number"), int)
        }
        per_pr: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for identifier, number in run_pr.items():
            if number in completed_numbers and identifier in runs_by_id:
                per_pr[number].append(runs_by_id[identifier])
        entry["prs_with_runs"] = len(per_pr)
        if per_pr:
            durations: list[float] = []
            run_counts: list[float] = []
            executed_counts: list[float] = []
            first_passes = 0
            for pr_runs in per_pr.values():
                pr_flags = {
                    int(run["id"]): _flags(run, superseded, abandoned_shas) for run in pr_runs
                }
                duration = sum(
                    (wall_seconds(run) or 0) / 60
                    for run in pr_runs
                    if not pr_flags[int(run["id"])]["zombie"]
                    and not pr_flags[int(run["id"])]["instant_skip"]
                    and not pr_flags[int(run["id"])]["watchdog"]
                )
                durations.append(duration)
                run_counts.append(float(len(pr_runs)))
                executed = [
                    run
                    for run in pr_runs
                    if not pr_flags[int(run["id"])]["zombie"]
                    and not pr_flags[int(run["id"])]["instant_skip"]
                    and not pr_flags[int(run["id"])]["watchdog"]
                    and not pr_flags[int(run["id"])]["qa_gate"]
                    and not pr_flags[int(run["id"])]["proof"]
                ]
                executed_counts.append(float(len(executed)))
                if not any(
                    is_failed(run) or int(run.get("run_attempt") or 0) >= 2 for run in executed
                ):
                    first_passes += 1
            entry["per_pr_duration_median_minutes"] = percentile(durations, 0.5)
            entry["per_pr_duration_p90_minutes"] = percentile(durations, 0.9)
            entry["per_pr_runs_median"] = percentile(run_counts, 0.5)
            entry["per_pr_runs_p90"] = percentile(run_counts, 0.9)
            entry["per_pr_runs_executed_median"] = percentile(executed_counts, 0.5)
            entry["first_pass_pr_share"] = round(first_passes / len(per_pr), 3)
        result[day.isoformat()] = {key: value for key, value in entry.items() if value is not None}
    return result


def workflow_aggregates(
    runs: list[dict[str, Any]], prs: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Compute trailing-window workflow fragility stats and execution percentiles."""
    abandoned_shas = {
        str(pr["head_sha"])
        for pr in prs
        if pr.get("merged_at") is None and isinstance(pr.get("head_sha"), str)
    }
    superseded = superseded_ids(runs)
    run_pr = attribute_runs(runs, prs)
    completed_with_runs = set(run_pr.values())
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[str(run.get("name") or "unnamed workflow")].append(run)
    result: dict[str, dict[str, Any]] = {}
    for workflow, group in grouped.items():
        flags = [_flags(run, superseded, abandoned_shas) for run in group]
        appeared = {run_pr[int(run["id"])] for run in group if int(run["id"]) in run_pr}
        entry: dict[str, Any] = {
            "runs": len(group),
            "failed_runs": sum(value["failed"] for value in flags),
            "self_healed_runs": sum(value["self_healed"] for value in flags),
            "retried_failed_runs": sum(value["retried_failed"] for value in flags),
            "cancelled_runs": sum(run.get("conclusion") == "cancelled" for run in group),
            "superseded_runs": sum(value["superseded"] for value in flags),
            "instant_skip_runs": sum(value["instant_skip"] for value in flags),
            "prs_appeared_on": len(appeared),
        }
        if completed_with_runs:
            entry["pr_appearance_share"] = round(len(appeared) / len(completed_with_runs), 3)
        if group:
            entry["retry_share"] = round(
                sum(int(run.get("run_attempt") or 0) >= 2 for run in group) / len(group), 3
            )
        execution = [
            wall
            for run in group
            if not is_zombie(run)
            if (wall := wall_seconds(run)) is not None and wall > _INSTANT_SKIP_SECONDS
        ]
        if execution:
            entry["exec_median_seconds"] = percentile(execution, 0.5)
            entry["exec_p90_seconds"] = percentile(execution, 0.9)
        result[workflow] = {key: value for key, value in entry.items() if value is not None}
    return result


def collect_repo(
    repo: str,
    days: list[date],
    now: datetime,
    tz: ZoneInfo,
    state_dir: Path,
) -> RepoCollection:
    """Fetch, merge, and recompute one repo without persisting partial state."""
    window_start = datetime.combine(days[0], day_time.min, tzinfo=tz).astimezone(UTC)
    cache_start = window_start - _CACHE_MARGIN
    cache = load_cache(_cache_path(state_dir, repo))
    watermark = parse_timestamp(cache.get("last_fetch_end"))
    fetch_start = (
        cache_start if watermark is None else max(cache_start, watermark - _INCREMENTAL_OVERLAP)
    )
    stats = FetchStats()
    fresh_runs = fetch_runs(repo, fetch_start, now, stats)
    fresh_prs = fetch_closed_prs(repo, fetch_start, stats)
    merged = merge_cache(
        cache,
        fresh_runs,
        fresh_prs,
        cache_start=cache_start,
        fetched_end=now,
    )
    runs = _runs_in_days(merged["runs"].values(), set(days), tz)
    prs = list(merged["prs"].values())
    attribution = attribute_runs(runs, prs)
    return RepoCollection(
        repo=repo,
        cache=merged,
        daily=daily_aggregates(runs, prs, days, tz),
        workflows=workflow_aggregates(runs, prs),
        window_runs=len(runs),
        window_prs=len(set(attribution.values())),
        api_requests=stats.api_requests,
    )


def build_snapshot(
    collections: Iterable[RepoCollection], days: list[date], now: datetime, tz: ZoneInfo
) -> dict[str, Any]:
    """Build the reconciliation JSON used by dry-run and persistent snapshots."""
    return {
        "version": 1,
        "generated_at": now.astimezone(UTC).isoformat(),
        "window": {
            "days": len(days),
            "start_day": days[0].isoformat(),
            "end_day": days[-1].isoformat(),
            "timezone": str(tz),
        },
        "repositories": {
            item.repo: {
                "days": item.daily,
                "workflows": item.workflows,
                "run": {
                    "window_days": len(days),
                    "window_runs": item.window_runs,
                    "window_prs": item.window_prs,
                    "api_requests": item.api_requests,
                },
            }
            for item in collections
        },
    }


def emit_snapshot(snapshot: dict[str, Any]) -> None:
    """Emit all absolute-state events after every repository has succeeded.

    Initialising here gives both a manual invocation and a schedule invocation
    the exporter process dimension: the schedule calls this module's ``main``
    rather than emitting a second, schedule-named event stream.
    """
    from shared import telemetry, telemetry_otlp

    telemetry.init_telemetry(process=PROCESS_NAME)
    telemetry_otlp.warmup()
    for repo, payload in snapshot["repositories"].items():
        for day, fields in sorted(payload["days"].items()):
            telemetry.emit(
                "telemetry", "ci_runs_daily", attributes={"repo": repo, "day": day, **fields}
            )
        for workflow, fields in sorted(payload["workflows"].items()):
            telemetry.emit(
                "telemetry",
                "ci_workflow_window",
                attributes={"repo": repo, "workflow": workflow, **fields},
            )
        telemetry.emit("telemetry", "ci_runs_run", attributes={"repo": repo, **payload["run"]})
    telemetry.sync()


def main(argv: list[str] | None = None) -> int:
    """Run the sampler, returning one on authoritative fetch failure."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--repo", action="append", default=None, help="owner/name to sample (repeatable)"
    )
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument(
        "--state-dir", type=Path, default=None, help="override $AVA_HOME/state/ci-runs"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print JSON without writes or emission"
    )
    parser.add_argument("--print-snapshot", action="store_true", help="print reconciliation JSON")
    args = parser.parse_args(argv)
    # Three default windows cap a cold walk's request budget while preserving
    # enough history for reconciliation beyond Prometheus's retention.
    if args.window_days < 1 or args.window_days > 90:
        parser.error("--window-days must be between 1 and 90")
    repos = args.repo or [DEFAULT_REPO]
    if len(set(repos)) != len(repos):
        parser.error("--repo values must be unique")
    tz = cluster_tz()
    now = datetime.now(UTC)
    days = complete_days(now, args.window_days, tz)
    if args.state_dir is None:
        from shared.paths import ava_home

        state_dir = ava_home() / _STATE_DIR_RELATIVE
    else:
        state_dir = args.state_dir
    try:
        collections = [collect_repo(repo, days, now, tz, state_dir) for repo in repos]
    except CiRunsError as exc:
        print(f"ci-runs-export: {exc}", file=sys.stderr)
        return 1
    snapshot = build_snapshot(collections, days, now, tz)
    if not args.dry_run:
        for item in collections:
            save_json(_cache_path(state_dir, item.repo), item.cache)
        save_json(state_dir / "snapshot.json", snapshot)
        emit_snapshot(snapshot)
    if args.dry_run or args.print_snapshot:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        print(
            f"ci-runs-export: {snapshot['window']['start_day']}..{snapshot['window']['end_day']} — "
            f"{sum(item.window_runs for item in collections)} runs, emitted"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
