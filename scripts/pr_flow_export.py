"""PR flow exporter — samples the Ava repo's merge flow into OTLP gauges.

Runs once a day on macmini (the registered machine that holds the `gh`
session and `~/.trunk/api-token`), recomputes the trailing window of the
fleet's pull-request pipeline, and publishes it to Prometheus + Loki through
the unified telemetry pipeline:

- ``pr_flow_daily`` — one event per complete day in the trailing window,
  re-emitted on every run so the whole window stays visible inside
  Prometheus's retention. Fields: ``merged_count`` plus (when the day has
  samples) ``ready_to_merge_median_seconds`` / ``ready_to_merge_p90_seconds``,
  ``qa_rounds_mean`` / ``qa_rereview_share``, ``flake_new_quarantines``.
- ``pr_flow_run`` — one event per run carrying the point-in-time
  ``queue_depth`` sample of the Trunk merge queue (absent when the queue is
  unreachable; the event itself stays as the daily breadcrumb).

Every numeric payload field is dispositioned as an ObservableGauge in
``shared/telemetry_otlp.py`` — the values are per-day absolute state, never
sums, and re-emission must replace them rather than accrue them.

Metric definitions (cluster-tz days; the fleet timezone is Asia/Shanghai):

- **ready -> merged**: for PRs whose ``merged_at`` falls on day D,
  ``merged_at - ready_at`` in seconds, where ``ready_at`` is the first
  ``ready_for_review`` timeline event, else ``created_at`` (the fleet opens
  PRs ready; the fallback keeps draft-style flows honest if one appears).
  median / p90 over the day's PRs, linear interpolation between closest
  ranks.
- **queue depth**: length of Trunk ``getQueue``'s ``enqueuedPullRequests``
  at run time. One sample per run — a point-in-time reading, not a per-day
  aggregate.
- **QA rounds**: per merged PR, the number of ``ava-qa`` receipt comments
  (``scripts/qa_receipt.py`` format, shared GitHub account); ``qa_rounds_mean``
  is the day's mean. A **delta re-review** is a receipt followed by a
  different head commit before the next receipt or merge — the brief's
  "post-receipt head-SHA change" — and ``qa_rereview_share`` is the share of the
  day's PRs with at least one.
- **flakes**: Trunk's flaky database — tests whose ``quarantined_at`` falls
  on day D, counted over the currently quarantined list.

Persistence (``$AVA_HOME/state/pr-flow/``, survives the prod source-tree
resets that clean untracked files out of the checkout):

- ``cache.json`` — per-PR compact records keyed by number, refreshed only
  when GitHub's ``updated_at`` moved (incremental cache; the run re-fetches a
  timeline only when the PR changed). This is the read path's whole write
  surface besides the snapshot.
- ``snapshot.json`` — the latest computed payload (window, days, run,
  stats), rewritten atomically each run. The reconciliation surface: rerun
  the script (``--print``) and diff against the emitted gauges.

Read-only by construction: GitHub is read through ``gh api`` GETs and Trunk
through its list endpoints (``getQueue`` / ``flaky-tests/list-quarantined-tests``);
nothing here writes to GitHub, Trunk, or any queue.

Rate-limit budget: a cold run fetches one list page per 100 closed PRs plus
one timeline per PR (at most ``_MAX_TIMELINE_PAGES`` pages each); steady
state refetches only PRs whose ``updated_at`` moved since the last run
(typically well under 100/day) plus ~24 list pages and one Trunk call per
100 quarantined tests. Measured on 2026-09-14 (30-day window, 1361 merged
PRs): a cold walk is ~1450 GitHub reads, steady state ~150-200/day — both
comfortably inside the account's 5000/hour budget.

Failure policy: the PR list is the authoritative population, so a failed
list fetch aborts the run (exit 1, no emission — old Prometheus values stay).
Timeline failures degrade to cached or partial records and are counted in
``stats``; Trunk failures drop only the fields that depend on them.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Script-mode path guards (PYTHONSAFEPATH=1 removed the implicit script-dir
# entry): this checkout's root first so `shared` resolves against this tree,
# then the scripts dir for the sibling scripts (ci_utils / qa_receipt).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

DEFAULT_REPO = "zhiyuan-zhang0206/Ava"
DEFAULT_WINDOW_DAYS = 30
PROCESS_NAME = "pr-flow"
TRUNK_TOKEN_PATH = Path.home() / ".trunk" / "api-token"

# The shared GitHub account that posts QA receipts (scripts/qa_receipt.py).
QA_ACCOUNT_ID = 87293881

_GH_TIMEOUT_S = 60.0
_TRUNK_REQUEST_TIMEOUT_S = 30.0
# Bounded pagination: the list caps this run's read budget; a truncated walk
# logs loudly rather than silently shrinking the window.
_MAX_LIST_PAGES = 80
_MAX_TIMELINE_PAGES = 3
_MAX_FLAKY_PAGES = 20
_PAGE_SIZE = 100

_STATE_DIR_RELATIVE = Path("state") / "pr-flow"


class PrFlowError(RuntimeError):
    """A fetch that the run cannot proceed past (the PR list)."""


@dataclass
class PrRecord:
    """One merged PR's pipeline record (the cache unit)."""

    number: int
    updated_at: str
    created_at: str
    merged_at: str
    ready_at: str
    receipts: list[dict[str, str]] = field(default_factory=list)
    head_deltas: int = 0
    partial: bool = False  # True when the timeline could not be read

    def to_cache(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "merged_at": self.merged_at,
            "ready_at": self.ready_at,
            "receipts": self.receipts,
            "head_deltas": self.head_deltas,
            "partial": self.partial,
        }

    @classmethod
    def from_cache(cls, number: int, data: dict[str, Any]) -> PrRecord:
        return cls(
            number=number,
            updated_at=str(data.get("updated_at") or ""),
            created_at=str(data.get("created_at") or ""),
            merged_at=str(data.get("merged_at") or ""),
            ready_at=str(data.get("ready_at") or ""),
            receipts=list(data.get("receipts") or []),
            head_deltas=int(data.get("head_deltas") or 0),
            partial=bool(data.get("partial")),
        )


@dataclass
class RunStats:
    """Fetch accounting for the snapshot and the PR description's bounds."""

    gh_api_calls: int = 0
    timeline_fetches: int = 0
    timeline_cache_hits: int = 0
    timeline_failures: int = 0
    trunk_calls: int = 0
    trunk_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "gh_api_calls": self.gh_api_calls,
            "timeline_fetches": self.timeline_fetches,
            "timeline_cache_hits": self.timeline_cache_hits,
            "timeline_failures": self.timeline_failures,
            "trunk_calls": self.trunk_calls,
            "trunk_error": self.trunk_error,
        }


def cluster_tz() -> ZoneInfo:
    """The cluster's wall clock — the day boundary every aggregate uses."""
    from shared.config import settings

    return ZoneInfo(settings.general.timezone)


def window_days(now: datetime, days: int, tz: ZoneInfo) -> list[date]:
    """The last `days` complete cluster-tz days, oldest first.

    Day D is complete once D+1 starts; the run at 00:2x on D+1 aggregates
    D (and the whole trailing window) with D's data final — ``merged_at``
    stamps the day, so a completed day never gains merges afterwards.
    """
    today = now.astimezone(tz).date()
    last_complete = today - timedelta(days=1)
    return [last_complete - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _run_gh(args: list[str], *, timeout: float = _GH_TIMEOUT_S) -> str:
    """Run one gh command and return stdout; raise on any failure.

    Direct `gh` (not the API wrapper) because the job runs where the gh
    session lives; the callers project fields with --jq so no payload class
    is parsed whole.
    """
    result = subprocess.run(  # noqa: S603 - fixed gh argv, no shell
        ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise PrFlowError(
            f"gh {' '.join(args[:4])} failed: {detail[-1] if detail else result.returncode}"
        )
    return result.stdout


def fetch_closed_prs(repo: str, since: datetime, stats: RunStats) -> list[dict[str, Any]]:
    """Closed PRs updated since `since`, newest-updated first (paginated).

    Sorted by ``updated`` desc, so the walk can stop at the first page whose
    tail predates the window. Only the fields the aggregates need are
    projected.
    """
    jq = "[.[] | {number, created_at, updated_at, merged_at}] | @json"
    prs: list[dict[str, Any]] = []
    for page in range(1, _MAX_LIST_PAGES + 1):
        out = _run_gh(
            [
                "api",
                f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page={_PAGE_SIZE}&page={page}",
                "--jq",
                jq,
            ]
        )
        stats.gh_api_calls += 1
        batch = json.loads(out)
        if not isinstance(batch, list):
            raise PrFlowError(f"unexpected pulls payload on page {page}")
        prs.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return prs
        oldest = min((str(item.get("updated_at") or "") for item in batch), default="")
        try:
            if oldest and datetime.fromisoformat(oldest) < since:
                return prs
        except ValueError:
            continue  # unparsable stamp: keep walking rather than stop early
    raise PrFlowError(
        f"pulls walk exceeded {_MAX_LIST_PAGES} pages — run budget exhausted, window truncated"
    )


def fetch_timeline(repo: str, number: int, stats: RunStats) -> list[dict[str, Any]]:
    """One PR's issue timeline, projected to the events the aggregates read.

    Bounded at ``_MAX_TIMELINE_PAGES`` pages; a busier timeline is truncated
    (logged via stats) rather than unbounded.
    """
    jq = (
        "[.[] | {event, created_at, label: (.label.name // null), sha: (.sha // null), "
        "commit_date: (.committer.date // .author.date // null), uid: (.user.id // null), "
        "body: (.body // null)}] | @json"
    )
    events: list[dict[str, Any]] = []
    for page in range(1, _MAX_TIMELINE_PAGES + 1):
        out = _run_gh(
            [
                "api",
                f"repos/{repo}/issues/{number}/timeline?per_page={_PAGE_SIZE}&page={page}",
                "--jq",
                jq,
            ]
        )
        stats.gh_api_calls += 1
        batch = json.loads(out)
        if not isinstance(batch, list):
            raise PrFlowError(f"unexpected timeline payload for PR #{number}")
        events.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    else:
        print(
            f"pr-flow: PR #{number} timeline truncated at {_MAX_TIMELINE_PAGES} pages",
            file=sys.stderr,
        )
    return events


def parse_receipts(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """QA receipts from a timeline: (at, head_sha, verdict) per receipt comment.

    The format is `scripts/qa_receipt.py`'s: a fenced ```ava-qa JSON block
    posted by the shared account. Malformed or foreign comments are skipped —
    the receipt count must never be inflated by a lookalike.
    """
    import qa_receipt

    receipts: list[dict[str, str]] = []
    for event in events:
        if event.get("event") != "commented" or event.get("uid") != QA_ACCOUNT_ID:
            continue
        body = event.get("body") or ""
        match = qa_receipt._RECEIPT.fullmatch(body)
        if match is None:
            continue
        try:
            value = json.loads(match[1])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        head_sha = value.get("head_sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            continue
        receipts.append(
            {
                "at": str(event.get("created_at") or ""),
                "head_sha": head_sha,
                "verdict": str(value.get("verdict") or ""),
            }
        )
    return receipts


def count_head_deltas(
    receipts: list[dict[str, str]],
    events: list[dict[str, Any]],
    merged_at: str,
) -> int:
    """Receipts followed by a different head before the next receipt / merge.

    The brief's delta re-review count: walk each receipt's window (its
    timestamp to the next receipt's, or to the merge) over the timeline's
    ``committed`` events; a commit whose sha differs from the receipt's head
    means the head moved and the review went stale.
    """
    committed = sorted(
        (str(event.get("commit_date") or ""), str(event.get("sha") or ""))
        for event in events
        if event.get("event") == "committed" and event.get("commit_date")
    )
    deltas = 0
    for index, receipt in enumerate(receipts):
        end = receipts[index + 1]["at"] if index + 1 < len(receipts) else merged_at
        moved = any(
            receipt["at"] < at < end and sha and sha != receipt["head_sha"] for at, sha in committed
        )
        if moved:
            deltas += 1
    return deltas


def build_record(meta: dict[str, Any], events: list[dict[str, Any]]) -> PrRecord:
    """The compact per-PR record from one PR's list row + timeline."""
    number = int(meta["number"])
    created_at = str(meta.get("created_at") or "")
    ready_at = created_at
    for event in events:
        if event.get("event") == "ready_for_review" and event.get("created_at"):
            ready_at = str(event["created_at"])
            break
    merged_at = str(meta.get("merged_at") or "")
    receipts = parse_receipts(events)
    return PrRecord(
        number=number,
        updated_at=str(meta.get("updated_at") or ""),
        created_at=created_at,
        merged_at=merged_at,
        ready_at=ready_at,
        receipts=receipts,
        head_deltas=count_head_deltas(receipts, events, merged_at),
    )


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method).

    n == 1 returns the single value; the caller only passes non-empty lists.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _duration_seconds(record: PrRecord) -> float | None:
    """ready -> merged seconds; None when missing/unparsable/non-positive.

    A zero-or-negative span is a data anomaly (a ready stamp after the merge
    stamp), not a real sample — it must not drag a percentile down.
    """
    if not record.ready_at or not record.merged_at:
        return None
    try:
        ready = datetime.fromisoformat(record.ready_at)
        merged = datetime.fromisoformat(record.merged_at)
    except ValueError:
        return None
    duration = (merged - ready).total_seconds()
    return duration if duration > 0 else None


def compute_days(
    records: list[PrRecord],
    days: list[date],
    tz: ZoneInfo,
    *,
    flake_counts: dict[date, int] | None,
) -> dict[str, dict[str, Any]]:
    """The per-day metric payload for the trailing window.

    Days with no merges keep ``merged_count = 0`` and omit the sample-based
    fields (an absent optional metric is not zero). ``flake_counts`` carries
    the flake source; None means the source was unreachable this run, and
    every day omits the flake field rather than claiming zero.
    """
    by_day: dict[date, list[PrRecord]] = {day: [] for day in days}
    for record in records:
        if not record.merged_at:
            continue
        try:
            merged_day = datetime.fromisoformat(record.merged_at).astimezone(tz).date()
        except ValueError:
            continue
        if merged_day in by_day:
            by_day[merged_day].append(record)

    payload: dict[str, dict[str, Any]] = {}
    for day in days:
        group = by_day[day]
        entry: dict[str, Any] = {"merged_count": len(group)}
        durations = sorted(
            value for record in group if (value := _duration_seconds(record)) is not None
        )
        if durations:
            entry["ready_to_merge_median_seconds"] = round(_percentile(durations, 0.5), 1)
            entry["ready_to_merge_p90_seconds"] = round(_percentile(durations, 0.9), 1)
        rounds = [len(record.receipts) for record in group]
        if rounds:
            entry["qa_rounds_mean"] = round(sum(rounds) / len(rounds), 3)
            entry["qa_rereview_share"] = round(
                sum(1 for record in group if record.head_deltas > 0) / len(group), 3
            )
        if flake_counts is not None:
            entry["flake_new_quarantines"] = flake_counts.get(day, 0)
        payload[day.isoformat()] = entry
    return payload


def load_trunk_token() -> str | None:
    """The Trunk API token from `~/.trunk/api-token`; None when absent.

    The file is the design's credential location (the macmini fact this job
    was scheduled around); a missing token skips the Trunk-sourced fields
    with a logged reason and never fails the run.
    """
    try:
        token = TRUNK_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


@contextlib.contextmanager
def _trunk_client() -> Any:
    """Import `scripts/ci_utils.py` (the repo's Trunk channel) lazily."""
    import ci_utils

    yield ci_utils


def fetch_queue_depth(repo: str, token: str, stats: RunStats) -> int | None:
    """The Trunk merge queue's pending depth right now; None on any failure."""
    with _trunk_client() as ci_utils:
        data, error = ci_utils._trunk_post("getQueue", ci_utils._trunk_target_payload(repo), token)
    stats.trunk_calls += 1
    if error is not None or data is None:
        stats.trunk_error = error or "no data"
        return None
    items = data.get("enqueuedPullRequests") or []
    return len(items) if isinstance(items, list) else None


def fetch_quarantined(repo: str, token: str, stats: RunStats) -> list[dict[str, Any]] | None:
    """Trunk's quarantined-test list (paginated); None on any failure.

    Pagination is the response's ``next_page_token`` chain: Trunk ignores a
    numeric ``page`` index here (it re-serves page one), so the token is the
    only walk that terminates on real data. The walk is capped at
    ``_MAX_FLAKY_PAGES`` pages; hitting the cap is logged rather than
    silently truncating the counts.
    """
    owner, name = repo.split("/", maxsplit=1)
    with _trunk_client() as ci_utils:
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_FLAKY_PAGES):
            page_query: dict[str, object] = {"page_size": _PAGE_SIZE}
            if page_token is not None:
                page_query["page_token"] = page_token
            payload: dict[str, object] = {
                "repo": {"host": "github.com", "owner": owner, "name": name},
                "org_url_slug": ci_utils._TRUNK_ORG_SLUG,
                "page_query": page_query,
            }
            data, error = ci_utils._trunk_post("flaky-tests/list-quarantined-tests", payload, token)
            stats.trunk_calls += 1
            if error is not None or data is None:
                stats.trunk_error = error or "no data"
                return None
            batch = data.get("quarantined_tests") or []
            if not isinstance(batch, list):
                stats.trunk_error = "unexpected quarantined_tests payload"
                return None
            collected.extend(item for item in batch if isinstance(item, dict))
            page = data.get("page") or {}
            page_token = page.get("next_page_token") if isinstance(page, dict) else None
            if not batch or not page_token:
                return collected
        print(
            f"pr-flow: quarantined walk capped at {_MAX_FLAKY_PAGES} pages; "
            "day counts are a lower bound",
            file=sys.stderr,
        )
        return collected


def count_quarantined_by_day(
    quarantined: list[dict[str, Any]], days: list[date], tz: ZoneInfo
) -> dict[date, int]:
    """New quarantines per day, from each entry's `quarantined_at` stamp."""
    counts: dict[date, int] = {}
    wanted = set(days)
    for entry in quarantined:
        raw = entry.get("quarantined_at")
        if not isinstance(raw, str):
            continue
        try:
            stamped = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            continue
        if stamped in wanted:
            counts[stamped] = counts.get(stamped, 0) + 1
    return counts


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    """The per-PR cache; a missing or corrupt file reads as empty (heals)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("prs") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write (tmp + rename) so a crash never leaves a torn file; a
    failed rename keeps the previous file and clears the stranded tmp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    try:
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def collect_records(
    prs: list[dict[str, Any]],
    window: list[date],
    tz: ZoneInfo,
    repo: str,
    cache: dict[str, dict[str, Any]],
    stats: RunStats,
    *,
    fetch_timeline_fn: Callable[[str, int, RunStats], list[dict[str, Any]]] | None = None,
) -> list[PrRecord]:
    """Merged-in-window PR records, refreshed only where `updated_at` moved.

    The cache is what bounds steady-state reads: an unchanged PR reuses its
    record. A changed PR refetches its timeline; a failed refetch falls back
    to the cached record when one exists (stale beats missing) and to a
    partial record otherwise — either way the failure is counted, never
    silently absorbed.
    """
    fetch = fetch_timeline_fn or fetch_timeline
    wanted = set(window)
    records: list[PrRecord] = []
    for meta in prs:
        merged_raw = meta.get("merged_at")
        if not isinstance(merged_raw, str) or not merged_raw:
            continue
        try:
            merged_day = datetime.fromisoformat(merged_raw).astimezone(tz).date()
        except ValueError:
            continue
        if merged_day not in wanted:
            continue
        number = int(meta["number"])
        cached_raw = cache.get(str(number))
        cached = PrRecord.from_cache(number, cached_raw) if cached_raw else None
        if (
            cached is not None
            and not cached.partial
            and cached.updated_at == str(meta.get("updated_at") or "")
            and cached.ready_at
        ):
            stats.timeline_cache_hits += 1
            records.append(cached)
            continue
        stats.timeline_fetches += 1
        try:
            events = fetch(repo, number, stats)
        except (PrFlowError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            stats.timeline_failures += 1
            print(f"pr-flow: timeline fetch failed for PR #{number}: {exc}", file=sys.stderr)
            if cached is not None and cached.ready_at:
                records.append(cached)
            else:
                records.append(
                    PrRecord(
                        number=number,
                        updated_at=str(meta.get("updated_at") or ""),
                        created_at=str(meta.get("created_at") or ""),
                        merged_at=merged_raw,
                        ready_at="",
                        partial=True,
                    )
                )
            continue
        records.append(build_record(meta, events))
    return records


def build_snapshot(
    *,
    repo: str,
    days: list[date],
    day_payload: dict[str, dict[str, Any]],
    run: dict[str, Any],
    tz: ZoneInfo,
    stats: RunStats,
    merged_in_window: int,
    partial: int,
    now: datetime,
) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": now.astimezone(UTC).isoformat(),
        "repo": repo,
        "window": {
            "days": len(days),
            "start_day": days[0].isoformat(),
            "end_day": days[-1].isoformat(),
            "timezone": str(tz),
        },
        "days": day_payload,
        "run": run,
        "stats": {
            "merged_in_window": merged_in_window,
            "partial_records": partial,
            **stats.to_json(),
        },
    }


def emit_snapshot(snapshot: dict[str, Any], *, dry_run: bool) -> None:
    """Emit the daily + run events through the unified telemetry pipeline."""
    if dry_run:
        return
    _emit_events(snapshot)


def _emit_events(snapshot: dict[str, Any]) -> None:
    """The pipeline write (separate seam so tests can assert dry-run silence)."""
    from shared import telemetry, telemetry_otlp

    telemetry.init_telemetry(process=PROCESS_NAME)
    telemetry_otlp.warmup()
    for day, entry in sorted(snapshot["days"].items()):
        attributes = {key: value for key, value in entry.items() if value is not None}
        telemetry.emit("telemetry", "pr_flow_daily", attributes={"day": day, **attributes})
    run_attributes: dict[str, Any] = {}
    if snapshot["run"].get("queue_depth") is not None:
        run_attributes["queue_depth"] = snapshot["run"]["queue_depth"]
    telemetry.emit("telemetry", "pr_flow_run", attributes=run_attributes)
    telemetry.sync()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to sample")
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="trailing complete days to sample (default %(default)s)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="snapshot/cache directory (default $AVA_HOME/state/pr-flow)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="compute + print, no writes, no emit"
    )
    parser.add_argument(
        "--print",
        dest="print_snapshot",
        action="store_true",
        help="print the snapshot JSON to stdout",
    )
    args = parser.parse_args(argv)
    if args.window_days < 1 or args.window_days > 90:
        parser.error("--window-days must be between 1 and 90")

    tz = cluster_tz()
    now = datetime.now(UTC)
    days = window_days(now, args.window_days, tz)
    since = datetime.combine(days[0], dtime(0, 0), tzinfo=tz).astimezone(UTC)
    stats = RunStats()

    prs = fetch_closed_prs(args.repo, since, stats)

    if args.state_dir is not None:
        state_dir = args.state_dir
    else:
        from shared.paths import ava_home

        state_dir = ava_home() / _STATE_DIR_RELATIVE
    cache = load_cache(state_dir / "cache.json")

    records = collect_records(prs, days, tz, args.repo, cache, stats)

    token = load_trunk_token()
    queue_depth: int | None = None
    flake_counts: dict[date, int] | None = None
    if token is None:
        stats.trunk_error = "no ~/.trunk/api-token"
        print("pr-flow: no Trunk token — queue depth / flake fields skipped", file=sys.stderr)
    else:
        queue_depth = fetch_queue_depth(args.repo, token, stats)
        quarantined = fetch_quarantined(args.repo, token, stats)
        if quarantined is not None:
            flake_counts = count_quarantined_by_day(quarantined, days, tz)

    day_payload = compute_days(records, days, tz, flake_counts=flake_counts)
    snapshot = build_snapshot(
        repo=args.repo,
        days=days,
        day_payload=day_payload,
        run={"queue_depth": queue_depth},
        tz=tz,
        stats=stats,
        merged_in_window=len(records),
        partial=sum(1 for record in records if record.partial),
        now=now,
    )

    if not args.dry_run:
        save_json(
            state_dir / "cache.json",
            {"version": 1, "prs": {str(r.number): r.to_cache() for r in records}},
        )
        save_json(state_dir / "snapshot.json", snapshot)
        emit_snapshot(snapshot, dry_run=False)

    if args.print_snapshot:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        window = snapshot["window"]
        print(
            f"pr-flow: {window['start_day']}..{window['end_day']} — "
            f"{snapshot['stats']['merged_in_window']} merged PRs, "
            f"{stats.timeline_fetches} timeline fetches, "
            f"{stats.timeline_cache_hits} cache hits, "
            f"queue_depth={queue_depth}, "
            f"{'dry-run (no writes, no emit)' if args.dry_run else 'emitted'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
