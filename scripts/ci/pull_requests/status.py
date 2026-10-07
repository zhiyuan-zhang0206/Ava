"""Read-only GitHub CI evidence and verdicts shared by monitors and owner tools."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

PASSING = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
FAILING = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)
CONFLICTING = frozenset({"CONFLICTING"})
_FALLBACK_REPO = "zhiyuan-zhang0206/Ava"

POLL_INTERVAL = 30
MAX_CONSECUTIVE_ERRORS = 3
_LIMBO_AGE_SECONDS = 600.0
TRUNK_MERGE_QUEUE_CHECK_NAME = "Trunk Merge Queue"
MAIN_CI_WORKFLOW_NAME = "CI"
CORE_SUITE_CHECK_PREFIXES = ("backend shard (", "e2e shard (")


class CIStatus(Enum):
    ALL_PASSED = "all_passed"  # every COMPLETED check is SUCCESS / SKIPPED / NEUTRAL
    FAILED = "failed"  # at least one COMPLETED check has a failing conclusion
    PENDING = "pending"  # some checks are still QUEUED / IN_PROGRESS / PENDING
    NOT_READY = "not_ready"  # draft PR: gate-skipped suite; one-shot and --wait exit 1
    MERGE_CONFLICT = "merge_conflict"  # PR has merge conflicts — CI blocked
    NO_CHECKS = "no_checks"  # rollup empty AND no run scheduled for the head
    NO_WORKFLOW_RUNS = "no_workflow_runs"  # checks exist, but Actions produced none
    ERROR = "error"  # gh CLI / network / JSON error

    @property
    def is_terminal(self) -> bool:
        """True for every settled verdict; PENDING alone is transitional."""
        return self is not CIStatus.PENDING


def _derive_repo() -> str:
    """owner/repo from the checkout's origin remote, else the fallback."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        m = re.search(r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?$", out)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except (OSError, subprocess.SubprocessError):  # noqa: S110 - best-effort lookup; fall back
        pass
    return _FALLBACK_REPO


DEFAULT_REPO = _derive_repo()


def _parse_ts(iso: str | None) -> float | None:
    """Parse a GitHub RFC3339 timestamp, returning None for bad probes."""
    try:
        return datetime.fromisoformat(iso.strip().strip('"')).timestamp() if iso else None
    except (AttributeError, TypeError, ValueError):
        return None


class LimboRun(TypedDict):
    """One run confirmed stuck in GitHub limbo (task #3275)."""

    id: int
    name: str
    age_s: int


@dataclass
class CIResult:
    verdict: CIStatus
    checks: list[dict] = field(default_factory=list)

    # Derived convenience fields (populated by check_ci)
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)  # {name, conclusion}
    workflow_checks: list[str] = field(default_factory=list)  # checks a workflow run produced
    # Trunk's queue-state check is likewise not a CI result and must not turn
    # an otherwise green PR into a perpetual PENDING verdict.
    trunk_checks: list[dict[str, Any]] = field(default_factory=list)
    # Runs stuck in GitHub limbo (task #3275): `queued`, zero jobs, aged past
    # `_LIMBO_AGE_SECONDS`. Detail on a PENDING verdict — never a basis for
    # green. Queued runs are missing execution evidence.
    limbo: list[LimboRun] = field(default_factory=list)
    mergeable: str = ""  # MERGEABLE / CONFLICTING / UNKNOWN
    error_detail: str = ""
    is_draft: bool = False  # PR draft state at this poll
    core_skipped: list[str] = field(default_factory=list)  # gate-skipped core check names

    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.verdict == CIStatus.MERGE_CONFLICT:
            return "PR has merge conflicts — rebase onto latest main first"
        if self.verdict == CIStatus.ALL_PASSED:
            return f"CI all green ({len(self.passed)} checks passed)"
        if self.verdict == CIStatus.FAILED:
            names = [c["name"] for c in self.failed]
            return f"CI FAILED: {', '.join(names)}"
        if self.verdict == CIStatus.NOT_READY:
            return f"CI NOT READY: draft PR — {_core_skip_note(self.core_skipped)}; not green — mark the PR ready for review to run CI"
        if self.verdict == CIStatus.PENDING:
            core = (
                f"CI pending: {len(self.pending)} still running ({', '.join(self.pending[:3])}...)"
                if len(self.pending) > 3
                else f"CI pending: {', '.join(self.pending)}"
            )
            if self.core_skipped:
                core += f" — {_core_skip_note(self.core_skipped)}"
            if self.limbo:
                stuck = ", ".join(
                    f"{r['name']} (#{r['id']}, {r['age_s'] // 60}m)" for r in self.limbo[:3]
                )
                core += f" — {len(self.limbo)} run(s) in GitHub limbo (queued, zero jobs): {stuck}"
            return core
        if self.verdict == CIStatus.NO_CHECKS:
            return "No checks found"
        if self.verdict == CIStatus.NO_WORKFLOW_RUNS:
            names = ", ".join(self.passed) or "none"
            return (
                f"CI DID NOT RUN: no workflow produced a check ({len(self.passed)} "
                f"non-workflow check(s) reporting: {names}). Not green — investigate "
                "why Actions did not schedule before merging."
            )
        return f"Error: {self.error_detail}"


def _repo_has_workflows() -> bool:
    """True when this checkout defines GitHub Actions workflows.
    Bounds the NO_WORKFLOW_RUNS guard to repos where workflow checks are actually expected — a
    repo with no workflows at all is legitimately green on app checks alone.
    """
    wf_dir = Path(__file__).resolve().parents[3] / ".github" / "workflows"
    return any(wf_dir.glob("*.yml")) or any(wf_dir.glob("*.yaml"))


def _core_suite_skipped(checks: list[dict]) -> list[str] | None:
    """Names of the core suite checks when every attached core check is SKIPPED."""
    core = [c for c in checks if str(c.get("name", "")).startswith(CORE_SUITE_CHECK_PREFIXES)]
    if not core or any(
        c.get("status") != "COMPLETED" or c.get("conclusion") != "SKIPPED" for c in core
    ):
        return None
    return [c["name"] for c in core]


def _draft_gate_skipped(result: CIResult, core_skipped: list[str] | None) -> bool:
    """True when a draft PR's core suite is entirely gate-skipped (not green)."""
    return result.is_draft and core_skipped is not None


def _core_skip_note(names: list[str]) -> str:
    """One clause naming the gate-skipped core jobs (shared by summaries/timeouts)."""
    shown = ", ".join(names[:3])
    extra = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
    return f"core CI jobs are skipped by draft gating ({shown}{extra}); the suite has not run for this head"


def _pending_reason(result: CIResult) -> str:
    """The --wait deadline reason; names the gate-skip when the core suite is skipped."""
    return (
        f"CI still pending — {_core_skip_note(result.core_skipped)}"
        if result.core_skipped
        else "CI still pending"
    )


def _latest_completed_per_name(checks: list[dict]) -> list[dict]:
    """Keep the newest COMPLETED check run per name.
    GitHub lists every check run on a commit, but branch protection and the required-status UI
    treat same-named runs as ONE logical check whose state is the newest COMPLETED run's. A stale
    CANCELLED run on the same SHA must not poison the verdict when a later run of the same name
    succeeded — a workflow with cancel-in-progress concurrency produces exactly this shape
    (2026-09-04: two CANCELLED runs of one check froze #1636).

    Commit statuses are exempt: GitHub already deduplicates StatusContext by
    context, and they carry `state`, not `status`/`conclusion`."""
    latest: dict[str, dict] = {}
    for c in checks:
        name = c.get("name")
        if not isinstance(name, str) or c.get("__typename") == "StatusContext":
            continue
        if c.get("status") != "COMPLETED":
            continue
        current = latest.get(name)
        if current is None:
            latest[name] = c
            continue
        if (_parse_ts(c.get("completedAt")) or 0) >= (_parse_ts(current.get("completedAt")) or 0):
            latest[name] = c
    kept: list[dict] = []
    for c in checks:
        name = c.get("name")
        if (
            c.get("__typename") == "StatusContext"
            or not isinstance(name, str)
            or c.get("status") != "COMPLETED"
            or latest.get(name) is c
        ):
            kept.append(c)
    return kept


def _partition_checks(checks: list[dict[str, Any]], result: CIResult) -> None:
    """Sort each rollup check into completed / passed / failed / pending on
    `result`, and record which of them a workflow produced.

    Only COMPLETED checks are judged: a QUEUED / IN_PROGRESS one is pending, and so is a COMPLETED
    one whose conclusion is unrecognized — guessing there is how a false "all green" gets reported.

    Trunk checks report queue state rather than CI results, so they are routed to a dedicated
    bucket and never enter the verdict fields.
    """
    for c in checks:
        if c.get("__typename") == "StatusContext":
            # Commit statuses have `context` + `state`, not `name` + `status` + `conclusion`.
            # Without this branch they read as a nameless "?" entry with a null status — an
            # eternal PENDING that froze every --wait watcher (2026-09-04: five PRs stalled with
            # all real checks green).
            context = c.get("context", "?")
            state = c.get("state", "")
            if state == "SUCCESS":
                result.completed.append(context)
                result.passed.append(context)
            elif state in ("FAILURE", "ERROR"):
                result.completed.append(context)
                result.failed.append({"name": context, "conclusion": state})
            else:
                result.pending.append(context)
            continue
        name = c.get("name", "?")
        status = c.get("status", "")
        conclusion = c.get("conclusion", "")

        if name.startswith(TRUNK_MERGE_QUEUE_CHECK_NAME):
            result.trunk_checks.append(c)
            continue
        # A check produced by a workflow run carries the workflow's name; checks posted by a GitHub
        # App (GitGuardian, coverage bots) leave it empty. This is what tells "the suite ran and
        # passed" apart from "the suite never started and an app happened to report".
        if c.get("workflowName"):
            result.workflow_checks.append(name)

        if status != "COMPLETED":
            result.pending.append(name)
            continue

        result.completed.append(name)
        if conclusion in FAILING:
            result.failed.append({"name": name, "conclusion": conclusion})
        elif conclusion in PASSING:
            result.passed.append(name)
        else:
            result.pending.append(name)


def _runs_not_yet_reporting(head_sha: str, repo: str | None) -> list[str] | None:
    """Names of workflow runs for `head_sha` that are scheduled but have not
    attached a check to the commit yet.

    `statusCheckRollup` cannot tell "Actions will never produce a check" from "Actions has not
    produced one *yet*": between a push and the first check-run appearing, both look like a rollup
    with no workflow checks in it. That gap is seconds wide and lands exactly on the first poll
    after a push, which is when an agent is most likely to be watching — and NO_WORKFLOW_RUNS reads
    as "stop, investigate".

    The runs API answers what the rollup cannot: a run in `queued` / `in_progress` / `requested` /
    `waiting` for this sha means checks are coming. An empty list means nothing is scheduled, which
    is the real failure the NO_WORKFLOW_RUNS guard exists for — as far as the API can see: a run
    that has not registered yet is invisible here, and registration lags a head change (seconds;
    longer when the queue is busy). The residual window and how callers should corroborate are
    documented at the NO_WORKFLOW_RUNS assignment in `check_ci`.

    `check_ci` probes this before every would-be-green verdict — not only when the rollup has no
    workflow checks — because a rollup with part of the suite attached and green is otherwise
    indistinguishable from a finished suite (2026-09-10: MonsoraV2 #774 reported ALL_PASSED while
    its main run sat queued). The empty-rollup branch probes it too: with no check attached yet, a
    run in flight is the only evidence that checks are coming at all.

    On any error this returns None — distinct from [] ("nothing is scheduled"), because an
    unanswerable probe must never read as green. The no-workflow-checks caller keeps its not-green
    NO_WORKFLOW_RUNS verdict either way; the all-passed caller reports ERROR (unknown) rather than
    guessing that nothing is coming.
    """
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={head_sha}"
            if repo
            else f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head_sha}",
            "--jq",
            '[.workflow_runs[] | select(.status != "completed") | .name] | @json',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        names = json.loads(r.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None
    return [str(n) for n in names] if isinstance(names, list) else None


def _main_workflow_run_completed(head_sha: str, repo: str | None) -> bool | None:
    """Whether the main CI workflow has a completed, non-skipped run for `head_sha`.
    The incomplete-runs probe cannot prove the suite was ever seen: seconds after a head change
    the rollup can carry a few second-scale checks — GitHub Apps and small proof workflows, all
    passing — while the main workflow's run has not registered yet and is therefore invisible to
    the runs API (2026-09-21, PR #3137: `--wait` read "CI all green (3 checks passed)" ~3s after
    the push; 18 checks were running seconds later). A green verdict needs positive evidence the
    main run actually ran for this head: a fully-skipped run is gated out, not evidence the suite
    was seen. Asking for a COMPLETED run keeps this probe race-free against the incomplete-runs
    probe: a run that registers between the two probes is still running, so the next poll reads it
    as PENDING rather than green.

    A passing check-set far smaller than the repo's suite is the tell to corroborate by hand (`gh
    run list --commit <head-sha>`) before trusting a green verdict from an unwatched context.

    None means the probe could not answer (missing evidence, never "no run"): the all-green caller
    reports ERROR (unknown) rather than guess green.
    """
    owner = repo if repo else "{owner}/{repo}"
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{owner}/actions/runs?head_sha={head_sha}&status=completed&per_page=100",
            "--jq",
            f'[.workflow_runs[] | select(.name == "{MAIN_CI_WORKFLOW_NAME}" and .conclusion != "skipped")] | length',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        count = int(r.stdout.strip())
    except ValueError:
        return None
    return count > 0


def _queued_run_candidates(owner: str, head_sha: str) -> list[Any] | None:
    """The head's `queued` runs as {id, name, created_at} rows; None when the probe cannot answer."""
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{owner}/actions/runs?head_sha={head_sha}",
            "--jq",
            '[.workflow_runs[] | select(.status == "queued") | {id, name, created_at}] | @json',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        candidates = json.loads(r.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None
    return candidates if isinstance(candidates, list) else None


def _run_job_count(owner: str, run_id: int) -> int | None:
    """How many jobs a run has; None when the count cannot be read."""
    jobs = subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{owner}/actions/runs/{run_id}/jobs", "--jq", ".total_count"],
        capture_output=True,
        text=True,
        check=False,
    )
    if jobs.returncode != 0:
        return None
    try:
        return int(jobs.stdout.strip())
    except ValueError:
        return None


def _limbo_runs(head_sha: str, repo: str | None) -> list[LimboRun] | None:
    """Runs for `head_sha` that look permanently stuck in GitHub limbo.
    The class (task #3275, 2026-09-13): `queued`, ZERO jobs ever created, and past
    `_LIMBO_AGE_SECONDS`. Such a run stays in every non-completed answer forever, so without this
    probe it holds an all-green rollup PENDING until the caller's timeout -- silently. None means
    the probe could not answer (missing evidence, never "no limbo"); a candidate whose job count
    cannot be read is skipped, not assumed stuck.
    """
    owner = repo if repo else "{owner}/{repo}"
    candidates = _queued_run_candidates(owner, head_sha)
    if candidates is None:
        return None
    now = time.time()
    limbo: list[LimboRun] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_id = candidate.get("id")
        created_at = candidate.get("created_at")
        created_ts = _parse_ts(created_at if isinstance(created_at, str) else None)
        if type(raw_id) is not int or created_ts is None:
            continue
        age = now - created_ts
        if age < _LIMBO_AGE_SECONDS:
            continue
        if _run_job_count(owner, raw_id) == 0:
            limbo.append(
                {"id": raw_id, "name": str(candidate.get("name") or "?"), "age_s": round(age)}
            )
    return limbo


def _empty_rollup_verdict(result: CIResult, head_sha: str, repo: str | None) -> None:
    """Resolve an empty `statusCheckRollup` into a verdict on `result`.
    The rollup alone cannot tell "Actions has not attached the first check of a run yet" from
    "nothing was scheduled for this head". The first is the state of every PR in the seconds after
    a push — exactly when an agent launches a watcher — and reading it as settled ends the watch
    before the suite has begun (2026-09-12: PR #2249 read NO_CHECKS 2s after the push; 16 checks
    were running 10s later). The runs API answers it: a run still scheduled for the head means
    checks are coming (PENDING); only a confirmed-empty answer is NO_CHECKS.

    An unanswerable probe (None) is ERROR, not NO_CHECKS — the same asymmetry the all-green path
    encodes: a gh / network failure must not produce the very settled NO_CHECKS the probe exists to
    rule out.
    """
    scheduled = _runs_not_yet_reporting(head_sha, repo)
    if scheduled is None:
        result.verdict = CIStatus.ERROR
        result.error_detail = (
            "runs API probe failed: cannot confirm whether checks for this head are still to attach"
        )
    elif scheduled:
        result.pending.extend(scheduled)
        result.verdict = CIStatus.PENDING
        result.limbo = _limbo_runs(head_sha, repo) or []
    else:
        result.verdict = CIStatus.NO_CHECKS


def _merge_conflict_result(data: dict, result: CIResult) -> CIResult:
    """Keep the conflict verdict's check detail without entering CI evaluation."""
    result.verdict = CIStatus.MERGE_CONFLICT
    checks = data.get("statusCheckRollup", [])
    result.checks = checks
    for c in checks:
        name = c.get("name", "?")
        status = c.get("status", "")
        if status == "COMPLETED":
            result.completed.append(name)
        else:
            result.pending.append(name)
    return result


def _attached_checks_verdict(result: CIResult, head_sha: str, repo: str | None) -> None:
    """Resolve the verdict once every attached check has passed and none is still pending."""
    # Every check attached so far has passed. Attached, though, is not the same as finished: a
    # run that is queued / in progress has attached nothing to this commit yet, so a rollup
    # carrying part of the suite — all green — is indistinguishable from a finished suite.
    # Multi-workflow repos on self-hosted runners sit in that window routinely (2026-09-10,
    # MonsoraV2 #774: ALL_PASSED in ~2s while ci.yml run 34487002348 was still queued), and a
    # green verdict there ends the watch early. Ask the runs API once, before any green
    # verdict, whether more is coming.
    scheduled = _runs_not_yet_reporting(head_sha, repo)
    if scheduled:
        result.pending.extend(scheduled)
        result.verdict = CIStatus.PENDING
        result.limbo = _limbo_runs(head_sha, repo) or []
    elif not result.workflow_checks and _repo_has_workflows():
        # Nothing from a workflow is attached — and nothing is confirmed scheduled (an
        # unanswerable probe, None, reads the same here) — while this checkout does define
        # workflows: the suite did not run. Reporting ALL_PASSED here is how a broken `runs-on`
        # — 2026-07-28, hosted runners a private repo could not schedule — reads as green: the
        # only check left standing was a GitHub App's, and it passed.
        #
        # Residual false positive (2026-09-12, task #3160): in the window right after a head
        # change (force-push or push), an app check has attached while the new run has not
        # *registered* yet — invisible to the runs probe, so a healthy PR can read as this
        # verdict once (2026-08-02 #1216; 2026-09-12: queue lag of minutes observed — the
        # attached app check is the tell). It is not proof the suite will not run: corroborate
        # head-precisely — `gh run list --commit <head-sha>` (add `--repo <owner/repo>` when
        # cwd is not the checkout) — a queued / in_progress run there means it is coming.
        # `--branch` is not an equivalent check: it also lists the runs of the head this one
        # replaced, and a force-push leaves those completed, which reads as either answer. The
        # genuine #885 shape stays distinguishable by re-checking after a pause; read later in
        # a watch, on a settled head, this verdict means what it says.
        result.verdict = CIStatus.NO_WORKFLOW_RUNS
    elif scheduled is None:
        # The attached checks all passed, but the probe could not answer whether more runs are
        # still queued. Unanswerable is not "nothing scheduled": report ERROR (unknown) —
        # --wait prints it and exits 3 if it persists — rather than guess green, and rather
        # than a PENDING that would claim checks are pending when none are.
        result.verdict = CIStatus.ERROR
        result.error_detail = (
            "runs API probe failed: cannot confirm no workflow run is still queued for this head"
        )
    else:
        # Nothing is still running — but that only rules out runs that HAVE registered.
        # Registration lags a head change, and in that window a rollup of second-scale checks
        # (GitHub Apps, small proof workflows) all passing is indistinguishable from a finished
        # suite: the incomplete-runs probe above sees nothing to wait for, so green here would
        # end a watch before the suite began (2026-09-21, PR #3137: "CI all green (3 checks
        # passed)" ~3s after the push; 18 checks were running seconds later). A green verdict
        # therefore also confirms the main CI workflow has been SEEN for this head — a
        # completed, non-skipped run of MAIN_CI_WORKFLOW_NAME — and a check-set far smaller
        # than the repo's suite is the tell to corroborate by hand. Scoped by
        # _repo_has_workflows(): a checkout with no workflows is legitimately green on app
        # checks alone.
        #
        # Not seen -> PENDING with the run name in `pending`: --wait keeps
        # polling (bounded by --timeout), and the one-shot keeps its legacy
        # contract — it prints this pending summary, never "all green".
        main_run = _main_workflow_run_completed(head_sha, repo) if _repo_has_workflows() else True
        if main_run is None:
            # Same asymmetry as every probe here: unanswerable is unknown,
            # never green (--wait exits 3 if it persists).
            result.verdict = CIStatus.ERROR
            result.error_detail = (
                "runs API probe failed: cannot confirm the main CI workflow "
                "has finished a run for this head"
            )
        elif not main_run:
            result.pending.append(MAIN_CI_WORKFLOW_NAME)
            result.verdict = CIStatus.PENDING
        else:
            # All completed, none failed, nothing left scheduled, and the
            # main workflow's run is seen.
            result.verdict = CIStatus.ALL_PASSED


def check_ci(pr_number: str | int, *, repo: str | None = None) -> CIResult:
    """Poll one PR's CI status and mergeability via `gh pr view --json`.
    Returns a CIResult with a clear verdict — no ambiguous exit codes that agents misinterpret.

    Merge conflict detection: queries ``mergeable`` alongside ``statusCheckRollup``.  When
    mergeable == "CONFLICTING" the verdict is MERGE_CONFLICT — CI runs are blocked until the
    conflict is resolved, so there is no point waiting.

    An empty rollup is probed against the runs API before it is called settled: a run still queued
    / in progress for this head means checks are coming (PENDING), a confirmed-empty answer is
    NO_CHECKS, and an unanswerable probe is ERROR — never a settled NO_CHECKS.

    Before a green verdict, a second probe confirms the main CI workflow has a completed,
    non-skipped run for this head — a check-set built from second-scale checks alone is not
    evidence the suite ran (2026-09-21, PR #3137).

    A draft PR whose core shard checks are all gate-skipped is NOT_READY before the green path:
    draft gating skipped the suite, so there is no CI evidence either way.

    The key rule: only COMPLETED checks are evaluated.  Checks that are QUEUED / IN_PROGRESS /
    PENDING are correctly identified as such and do NOT trigger a FAILED verdict.
    """
    pr_num = str(pr_number)
    result = CIResult(verdict=CIStatus.ERROR)

    # No --repo => gh resolves the PR against the current checkout's remote, so
    # this works in any clone or fork without a hard-coded slug.
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "pr",
            "view",
            pr_num,
            *(("--repo", repo) if repo else ()),
            "--json",
            "mergeable,statusCheckRollup,headRefOid,isDraft",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        result.error_detail = f"gh CLI error: {r.stderr.strip()}"
        return result

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        result.error_detail = f"JSON parse error: {e}"
        return result

    # --- Merge conflict detection ---
    result.mergeable = data.get("mergeable", "UNKNOWN")
    result.is_draft = bool(data.get("isDraft", False))
    if result.mergeable in CONFLICTING:
        return _merge_conflict_result(data, result)

    # --- CI check evaluation ---
    checks = _latest_completed_per_name(data.get("statusCheckRollup", []))
    result.checks = checks

    if not checks:
        # An empty rollup is probed before it is called settled: only a
        # confirmed-empty runs API makes NO_CHECKS true (the attach window right
        # after a push looks identical to it — 2026-09-12, PR #2249).
        _empty_rollup_verdict(result, data.get("headRefOid", ""), repo)
        return result

    core_skipped = _core_suite_skipped(checks)
    result.core_skipped = core_skipped or []
    _partition_checks(checks, result)

    # Determine verdict
    if result.failed:
        result.verdict = CIStatus.FAILED
    elif _draft_gate_skipped(result, core_skipped):
        result.verdict = CIStatus.NOT_READY
    elif result.pending:
        result.verdict = CIStatus.PENDING
    else:
        _attached_checks_verdict(result, data.get("headRefOid", ""), repo)

    return result
