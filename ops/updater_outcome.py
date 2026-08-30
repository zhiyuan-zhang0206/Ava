"""What this host's last updater session did, read back off disk after the fact.

The updater is a detached session, so the process that knows how the self-update
ended is gone by the time anyone asks — and the thing that would most like to ask
is on another machine. Phase B of a rollout acks the *spawn*; the exit code lands
only in `$AVA_HOME/logs` on the host. So the orchestrator's `POLL_STALLED` says
"this host stopped and did not resume" and stops exactly one level short of the
fact an operator needs: a preflight that **refused** left the host fully intact
and serving its old code, while an updater that **died** may have moved the
checkout and not the processes. Same label, opposite next actions, and the only
way to tell them apart was to ssh in and read a log — on Windows, where that is
hardest, and for the case where nothing is actually broken.

This reads the log rather than having the updater write a record, because a
record written anywhere else is a second thing that can be missing: the markers
the updater already prints are on both platforms, and reading them changes
nothing about the deploy path — which #995 requires: nothing retries, nothing is
reaped differently, no host is treated differently.

**Both platforms write the exit verdict now** (`native_exit_line`). They arrive
at it differently: the POSIX chain expands `$?` once at its end, while the
cmd.exe command line has no `$?` it can expand there without delayed expansion —
so every terminal branch of the Windows ladder states its own literal rc instead
(`ops._update_shell._restart_recovery_cmd`). That closes the gap this module used
to have to describe rather than answer: a Windows host reading `unknown` meant
"either it died mid-flight or this platform simply never says", one label over
two opposite situations, on the platform hardest to ssh into. `unknown` now
carries only the first reading — with one rollout of imprecision, the one that
ships this, where a Windows host still runs the old chain from before its own
checkout moved.

**Staleness is not optional.** The log directory always holds a previous update's
log, and on Windows the supervisor appends every run to one file — so "the newest
updater log" is an answer even when this update never wrote a line. A stale rc=0
read as this update's outcome is worse than no answer, so the read is anchored to
the pause: `pause_local_cluster()` stamps `host_deploy_state.paused_at`
immediately before `spawn_update` opens the log (the retired `cluster_paused`
file's mtime, R1 old-signal sweep), and a stalled host is by definition still
paused, so a log last written *before* that anchor belongs to an earlier update
and is reported as no record at all.

**On Windows that anchor dates the FILE, not the lines in it** — one file, every
run — so a bounded tail could still hold the last lines of a PREVIOUS run beside
this one's, and a decline marker anywhere in it was read as this run's verdict
(issue #1117): a host half-transitioned by a newer run reported as untouched and
still serving its old code. The updater's cmd.exe command line therefore echoes a
per-run start marker (`mark_native_run`) before it does anything else, and the
read slices the tail at that marker (`_anchor_to_this_run`). POSIX writes no
marker — its per-spawn `updater-<epoch>.log` already holds exactly one run — so
the slice there is the whole tail and nothing about that platform changes.
"""

from __future__ import annotations

import re
import time
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, Field

import shared.paths
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S, STAGE_NO_PROGRESS_TIMEOUT_S
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
from shared.platform import IS_WINDOWS

# How much of the log's tail to classify. The markers are the last few lines of a
# finished run; reading the whole file would mean loading an arbitrarily large log
# into an ops-server request.
#
# **Arbitrarily large is a Windows fact, and the tail there can span runs.** POSIX
# gets a fresh `updater-<epoch>.log` per spawn (`_new_update_log`), so its file holds
# exactly one run and stays small — the bound is a formality. Windows has no per-run
# file: the supervisor appends every run to one `ava-updater.out.log` that grows
# without bound, so the bound is load-bearing there (slurping a multi-MB log on every
# status_probe would slow the probe the whole rollout poll waits on), and the tail can
# hold the last lines of a PREVIOUS run alongside this one's. `_anchor_to_this_run` is
# what separates them, and the two bounds are a pair: the marker it anchors on is the
# run's FIRST line, so it is in the tail exactly while this run has written less than
# `_TAIL_BYTES` — and when it has written more, the tail is this run's output alone.
# The two cases are exhaustive at any value of this bound, which is why the marker
# being missing is a sound reading rather than a fallback (`_anchor_to_this_run`).
_TAIL_BYTES = 8192

# The markers the updater already prints. Both travel on both platforms: the
# decline sentence is echoed by the POSIX entry and by the cmd.exe ladder's rc=3
# branch, and the exit line is written by `native_exit_line` below.
_DECLINE_MARKER = "restart DECLINED by its own preflight"
_EXIT_MARKER = "[session-exit] rc="

# The per-run start marker, emitted by `spawn_update`'s cmd.exe command line only.
# Windows' log holds every run, so this is the one thing in it that says where a run
# begins; POSIX needs none (one file per spawn) and prints none, which is what keeps
# that platform's log byte-identical.
_RUN_MARKER = "[updater-run]"

# The stage lines the updater writes (Task #1820 — per-host stage telemetry). Two
# shapes, one regex: the in-process legs print `dur=` at the stage's end
# (`shared.rollout_telemetry.updater_stage`), the cmd.exe ladder prints `t=` at the
# stage's start (`cli.commands._updater_stage`), and `_parse_stages` pairs
# consecutive `t=` markers into durations. A stage line in the log that fails to
# match is a line nothing reads — which is why the emitter and this regex are
# asserted against each other in tests.
_STAGE_LINE_RE = re.compile(
    r"\[updater\] stage=([a-z0-9_]+) (?:dur=([0-9]+(?:\.[0-9]+)?)s|t=([0-9]+(?:\.[0-9]+)?))"
)


def _parse_stages(tail: str) -> dict[str, float]:
    """Per-stage durations from the updater log's `[updater] stage=` lines.

    `dur=` lines are self-contained (in-process legs). `t=` markers (the cmd.exe
    ladder) carry a monotonic timestamp instead, so consecutive markers are paired
    into durations — the last marker's own duration is unknowable from markers
    alone (its step is the run's tail), which is why the ladder's final step
    (`ava restart`) also writes `dur=` lines from inside `cmd_restart`.

    A stage that appears more than once keeps its LAST `dur=` value, and a `t=`
    marker never overwrites a `dur=` value for the same name. Durations round to
    0.1s — the brief's 368s rollout is decomposed to that precision, and no
    consumer here needs more.
    """
    stages: dict[str, float] = {}
    markers: list[tuple[str, float]] = []
    for match in _STAGE_LINE_RE.finditer(tail):
        name, dur, t = match.group(1), match.group(2), match.group(3)
        if dur is not None:
            stages[name] = round(float(dur), 1)
        else:
            markers.append((name, float(t)))
    for (name, t0), (_next, t1) in pairwise(markers):
        stages.setdefault(name, round(t1 - t0, 1))
    return stages


def _parse_stage_evidence(tail: str) -> tuple[str | None, float | None]:
    """The stage named by `tail`'s LAST `t=` marker and how long it has been in
    flight, or (None, None) when the tail's last stage line is a `dur=` (that stage
    completed) or there is no stage line at all.

    A `t=` marker is written at a stage's START on both platforms — the cmd.exe
    ladder's `cli.commands._updater_stage` between steps, the in-process legs'
    `shared.rollout_telemetry.updater_stage` at entry — so a tail whose last stage
    line is a `t=` names the stage the updater is inside right now. The age is
    computed against this process's monotonic clock at read time: the same clock,
    on the same host, that stamped the marker (the updater wrote it, the ops server
    reads it), so the subtraction is sound across the two processes that bookend it.

    The ONE boundary where a lingering `t=` tail does not mean "in flight" is the
    final `done` marker, which the ladder prints after the restart has already
    converged the host — a reader that acts on it must gate on posture first (the
    Phase-B poll checks idle before any stage evidence; the host reaper refuses
    stage judgments on an idle host).
    """
    last_marker: tuple[str, float] | None = None
    for match in _STAGE_LINE_RE.finditer(tail):
        if match.group(3) is not None:
            last_marker = (match.group(1), float(match.group(3)))
        else:
            last_marker = None
    if last_marker is None:
        return None, None
    name, stamped = last_marker
    return name, round(time.monotonic() - stamped, 1)


# Lines worth carrying off the box: the preflight's own `✗` complaints (which name
# the gateway URL that was unreachable) and the updater's own narration.
_DETAIL_PREFIXES = ("✗", "[updater]")
_DETAIL_LINES = 3


class UpdaterOutcome(BaseModel):
    """How this host's last updater session ended, as far as its log can say.

    `kind` is the operator-facing distinction:

    - `declined` — the restart's validate-before-kill preflight refused. **Nothing
      was stopped**: the host is serving its old code, intact, and the next action
      is to fix what the preflight named (usually gateway reachability) and re-run.
    - `exited` — the session ran to its end and reported `rc`. Non-zero means the
      self-update failed after the stop; the host may be half-transitioned.
    - `unknown` — the log is this update's, and carries neither marker. The
      session died mid-flight: every terminal branch on both platforms writes one
      (`native_exit_line`), so reaching an end and saying nothing is no longer a
      thing a healthy run does.

    `rc` is None whenever no exit line was found. It is a fact about the run, not
    about the platform: a Windows run that got as far as any branch of its ladder
    reports one.
    """

    # Defaulted, not required, because this is reconstructed from the wire by the
    # rollout orchestrator — which is on a different commit from the runner that
    # sent it for the whole duration of a rollout, by construction. A report
    # renderer that raised on a field an older/newer host phrased differently would
    # take down the report that exists to explain a failure.
    kind: str = "unknown"
    rc: int | None = None
    detail: str = ""
    log: str = ""
    # Per-stage durations parsed from the log's `[updater] stage=` lines
    # (Task #1820): the Windows ladder's fetch/checkout/uv markers and the
    # in-process legs' checkout/uv/stop/start lines, as seconds. Empty on a log
    # that predates the markers or a run that never wrote one. Best-effort
    # observational data — it rides the status probe, so a missing or partial
    # dict is a gap in the report, never a reason to refuse one.
    stages: dict[str, float] = Field(default_factory=dict)
    # The stage the updater is inside RIGHT NOW, and how long it has been there:
    # the tail's last `t=` marker and its age against this process's monotonic
    # clock at read time (see `_parse_stage_evidence`). None when the last stage
    # line is a completed `dur=` (nothing in flight) or the log predates the
    # markers. This is the no-progress evidence (P1, 2026-08-30): the host's
    # hung-updater reaper and the Phase-B poll compare it against
    # STAGE_NO_PROGRESS_TIMEOUT_S. A host answering the probe from an older
    # commit reports neither field — which reads as "cannot tell", never as
    # progress, so the judgment simply waits for the rollout that ships it.
    current_stage: str | None = None
    current_stage_s: float | None = None


def updater_log_candidates(session: str) -> list[Path]:
    """Every file `session`'s output could be landing in, newest-relevant first.

    **The platforms have different authoritative logs.** The POSIX updater pipes itself through
    ``tee -a $AVA_HOME/logs/updater-<epoch>.log``; the Windows supervisor owns the
    redirect instead and appends to ``$AVA_HOME/logs/ava-updater.out.log``
    (`SessionBackend.session_log_path`), so the `updater-*.log` glob **is never
    written on Windows** — verified on the fleet box: `spawn_update` logs that
    path, and no file by that name exists in the log directory.

    A POSIX tee file holds exactly one run, so it is authoritative whenever it
    exists. On 2026-08-24, appending the shared backend log let its microscopically
    newer mtime select a previous run's ``rc=0`` while the tee file still held this
    run's partial output; Phase B then falsely declared the live updater stalled.
    The backend log remains the fallback without a tee file and the Windows source,
    whose per-run marker is narrowed by `_anchor_to_this_run`.
    """
    from shared.session_backend import get_backend

    per_run_logs = sorted((shared.paths.ava_home() / "logs").glob("updater-*.log"))[-1:]
    candidates = list(per_run_logs)
    try:
        backend_log = get_backend().session_log_path(session)
    except Exception:  # fail-fast-ok: backend unavailable — the tee'd log is all we have
        backend_log = None
    if backend_log is not None and (IS_WINDOWS or not per_run_logs):
        candidates.append(backend_log)
    return candidates


def _run_marker_line(epoch: int) -> str:
    """The marker line for a run spawned in second `epoch`.

    The epoch is what makes the marker hard to fabricate: a line that merely *looks*
    like one (a commit subject echoed by `git checkout`, say) carries whatever epoch
    it was written with, and `_anchor_to_this_run` only accepts one at least as new
    as the current pause.
    """
    return f"{_RUN_MARKER} {epoch}"


def mark_native_run(native_cmd: str) -> str:
    """`native_cmd` with this run's start marker echoed ahead of everything it does.

    The emitter lives beside the parser because the two must agree on more than a
    string: `_marker_epoch` — the rule that decides every healthy run — accepts a
    marker only at the start of a line, which is a property of *how* it is printed.
    Split across modules, a marker spelled or printed differently is a marker
    nothing anchors on, and that fails silently, as exactly the misattribution this
    exists to prevent.

    Only the native (cmd.exe) command line is marked. POSIX gets a fresh
    `updater-<epoch>.log` per spawn, so it has nothing to separate, and leaving its
    command line alone is what keeps that platform's log byte-identical.

    One deliberate side effect: this is the run's first write, so it stamps the
    shared log's mtime at spawn — the historical evidence the hung-updater reaper
    used before liveness became the updater lease (`updater_reap._updater_hung`,
    R1 old-signal sweep PR5). A native run therefore reads as alive from its own
    beginning rather than from whenever the *previous* run last wrote, which is the
    reading that was wanted anyway: a young updater blocked on its first `git fetch`
    used to be indistinguishable from a dead one.
    """
    return f"echo {_run_marker_line(int(time.time()))} & {native_cmd}"


def native_exit_line(rc: int) -> str:
    """The cmd.exe fragment that states a run's verdict — `echo [session-exit] rc=N`.

    Here for the same reason as `mark_native_run`: the emitter and `_classify`'s
    parse of `_EXIT_MARKER` have to agree, and a marker spelled differently is a
    marker nothing reads — which fails silently, as the very "no exit verdict"
    answer this closes.

    `rc` is a literal because cmd.exe cannot expand the errorlevel at the end of a
    command line without delayed expansion. Each terminal branch of the ladder
    therefore states the rc its own meaning implies (`_restart_recovery_cmd`),
    rather than reporting one it captured. The POSIX chain keeps expanding `$?`
    and does not go through here — its verdict is a real exit code, and nothing
    about that platform's log changes.
    """
    return f"echo {_EXIT_MARKER}{rc}"


def _epoch_after_marker(text: str) -> int | None:
    """The epoch `text` carries, given that it opens with the marker literal."""
    try:
        return int(text[len(_RUN_MARKER) :].split(maxsplit=1)[0])
    except (IndexError, ValueError):
        return None


def _marker_epoch(line: str) -> int | None:
    """The epoch of `line` if it is a run marker at **column zero**, else None.

    `cmd /c "echo <marker> & …"` puts it there, and the text this reader does not
    control does not go there unprefixed (`git checkout` writes `HEAD is now at <sha>
    <subject>`, `uv sync` indents its package lines). Only the trailing end is
    forgiven, because that line arrives CRLF terminated with a space before it:
    cmd.exe echoes the space ahead of the `&` too.
    """
    trimmed = line.rstrip()
    if not trimmed.startswith(_RUN_MARKER):
        return None
    return _epoch_after_marker(trimmed)


def _embedded_marker_epoch(line: str) -> int | None:
    """The epoch of the last marker literal anywhere in `line`, else None.

    Column zero is the rule; this is what is asked only once the rule has found
    nothing (`_anchor_to_this_run`). It exists for one shape — a marker glued onto a
    previous run's half-written line — so it takes the LAST literal in the line: if a
    line somehow carried two, the later one is the one nearer this run.
    """
    at = line.rfind(_RUN_MARKER)
    if at < 0:
        return None
    return _epoch_after_marker(line[at:].rstrip())


def _anchor_to_this_run(tail: str, paused_at: float) -> str:
    """`tail` narrowed to the lines this update's own run wrote.

    Windows appends every run to one file, so the tail can open in the middle of a
    previous run. Slicing at the last start marker that is not older than
    `paused_at` — the pause moment (posture row, ex-`cluster_paused` file mtime)
    this update took immediately before spawning — leaves only lines written
    after this run began.

    **Two passes, and the order is the safety property.** The primary rule is a
    marker at column zero, which is where the updater's own `echo` puts it. The
    fallback then accepts a marker anywhere in a line, because one real shape denies
    the run column zero entirely: a previous run force-killed mid-write (which is what
    the hung-updater reaper does) leaves its last line in the shared file with no
    newline, so this run's marker is appended into the MIDDLE of it. Without the
    fallback that tail anchors nowhere and the killed run's verdict is read as this
    one's — the misattribution this function exists to prevent, arriving by another
    route.

    **The epoch is what makes the loose pass safe, and it is not optional there.** A
    mid-line match is text this reader does not control — a commit subject travelling
    verbatim through `git checkout` is the realistic one — so it only counts if it
    carries an epoch at least as new as this update's pause, which such text does not:
    it was written before the pause it would have to postdate, or it carries no
    parseable epoch at all. What remains is a subject that embeds a *future* epoch,
    which is adversarial rather than accidental, and its whole power is to truncate a
    diagnostic window on a host already being updated.

    **No qualifying marker in either pass is not a failure to anchor.** The marker is
    the run's first line, so its absence means the run has already written more than
    `_TAIL_BYTES`, and a tail that far into a single run cannot reach back into the
    previous one. The one case where that reasoning does not hold is an updater
    spawned by code that predates the marker (the rollout that ships it), which
    reads exactly as it did before — the approximation, not something worse.
    """
    lines = tail.splitlines()
    # `int(paused_at)`: the marker's epoch is truncated to the second, so a pause at
    # 1000.7 and a spawn at 1000.9 would otherwise read as marker-before-pause.
    floor = int(paused_at)
    for read_epoch in (_marker_epoch, _embedded_marker_epoch):
        for index in range(len(lines) - 1, -1, -1):
            epoch = read_epoch(lines[index])
            if epoch is not None and epoch >= floor:
                return "\n".join(lines[index + 1 :])
    return tail


def _newest_log(session: str) -> Path | None:
    live = [p for p in updater_log_candidates(session) if p.exists()]
    if not live:
        return None
    return max(live, key=lambda p: p.stat().st_mtime)


def _classify(tail: str, log: Path) -> UpdaterOutcome:
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    # Stage lines are telemetry, not narration: they start with `[updater]` (one of
    # the detail prefixes) but carry only `stage=... dur=...` — keeping them in
    # `detail` would hand the operator a wall of stage numbers where the preflight
    # complaint used to be.
    detail = " | ".join(
        line
        for line in lines
        if line.startswith(_DETAIL_PREFIXES)
        and _EXIT_MARKER not in line
        and not line.startswith("[updater] stage=")
    )
    rc: int | None = None
    for line in reversed(lines):
        if _EXIT_MARKER in line:
            try:
                rc = int(line.split(_EXIT_MARKER, 1)[1].split()[0])
            except (IndexError, ValueError):
                rc = None
            break
    tail_detail = " | ".join(detail.split(" | ")[-_DETAIL_LINES:])
    if _DECLINE_MARKER in tail:
        kind = "declined"
    elif rc is not None:
        kind = "exited"
    else:
        kind = "unknown"
    current_stage, current_stage_s = _parse_stage_evidence(tail)
    return UpdaterOutcome(
        kind=kind,
        rc=rc,
        detail=tail_detail,
        log=log.name,
        stages=_parse_stages(tail),
        current_stage=current_stage,
        current_stage_s=current_stage_s,
    )


def last_updater_outcome() -> UpdaterOutcome | None:
    """This host's last updater outcome, or None when no log speaks for *this* update.

    None covers three readings that an operator must not be handed as an outcome: no
    updater log exists at all, the newest log is stale, and the host is neither
    paused nor freshly-idle (no update in flight and none just finished). The pause
    is the anchor for a paused/converging host; a freshly-idle host keeps the
    reading for a while (NO_PROGRESS_TIMEOUT_S window) so the rollout poll can
    harvest the completed stage breakdown after convergence — Task #1820.

    The same flag anchors the tail a second time, one level finer: on a log that
    holds every run (Windows), `_anchor_to_this_run` keeps only the lines after this
    run's own start marker, so a previous run's decline is not classified as this
    one's.

    Never raises. It is called from `status_snapshot`, which answers the probe the
    whole rollout poll depends on — a status endpoint that 500s because a log line
    could not be parsed would turn a diagnosable stall into an unreachable host.
    """
    from ops.cluster_session import _UPDATER_SERVICE
    from shared.cluster import session_name
    from shared.host_deploy_state import POSTURE_IDLE, read

    try:
        # The pause anchor lives in the posture row (R1 old-signal sweep, PR5):
        # `paused_at` is the moment the current pause window started — written
        # at the pause transition and preserved through `converging` — standing
        # in for the retired `cluster_paused` file's mtime. A host with no row
        # has never been paused; `paused_at` NULL means the window's anchor is
        # gone (idle, or an updater that ran without a pause).
        state = read()
        if state is None:
            return None
        if state.paused_at is not None:
            paused_at = state.paused_at.timestamp()
        else:
            # Fresh-idle reading (Task #1820): a finished update clears the
            # pause anchor on resume, so an idle host has none — but the
            # Phase-B poll needs the host's COMPLETED stage breakdown after
            # convergence, and the updater's final `start` stage line lands
            # only after the posture row has already gone idle. The freshness
            # window stands in for the anchor: the family's no-progress bound
            # (shared/deploy_timing) is the longest an updater run can span, so
            # a log written within it belongs to THIS host's just-finished
            # update, and `_anchor_to_this_run` still slices it by run marker.
            if state.posture != POSTURE_IDLE:
                return None
            paused_at = time.time() - NO_PROGRESS_TIMEOUT_S
        log = _newest_log(session_name(_UPDATER_SERVICE))
        if log is None or log.stat().st_mtime < paused_at:
            return None
        with log.open("rb") as fh:
            fh.seek(max(0, log.stat().st_size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
        return _classify(_anchor_to_this_run(tail, paused_at), log)
    except Exception:  # fail-fast-ok: a diagnostic must never break the status probe
        import logging

        logging.getLogger(__name__).warning("last_updater_outcome read failed", exc_info=True)
        return None


def updater_outcome_declined(outcome: UpdaterOutcome | None) -> bool:
    """Whether this outcome is a preflight refusal rather than a failure.

    The one distinction that changes what the operator does next, so it is asked as
    its own question instead of being re-derived from `kind` at each call site. Both
    spellings count: the decline marker (the only signal Windows writes) and the
    POSIX exit line carrying `RESTART_DECLINED_EXIT_CODE`.
    """
    if outcome is None:
        return False
    return outcome.kind == "declined" or outcome.rc == RESTART_DECLINED_EXIT_CODE


# What to do about a refusal, appended to every rendering of one. The preflight
# refuses on its own probes, and the probe that fails in the shape #995 was filed
# for is gateway reachability — so the fix is the operator's, not the host's, and
# naming it is the difference between a verdict and a next step.
_DECLINE_NEXT_STEP = (
    "fix what the preflight named (usually gateway reachability) and re-run the update"
)


def _stages_clause(outcome: UpdaterOutcome) -> str:
    """` (stages: fetch 3.1s, checkout 1.2s, ...)` for an outcome that has them.

    The clause is appended to every rendered outcome, because the stage times are
    the answer to a question the verdict itself leaves open: WHERE the time went.
    The brief's Windows case — a 75.9s updater decision with no way to subdivide
    checkout/uv/stop/start — is exactly this clause rendered. Sorted so a reader
    comparing hosts reads the same order.
    """
    if not outcome.stages:
        return ""
    parts = ", ".join(f"{name} {dur:.1f}s" for name, dur in sorted(outcome.stages.items()))
    return f" (stages: {parts})"


def describe_updater_outcome(outcome: UpdaterOutcome | None) -> str:
    """The clause a stalled host gets in the rollout report.

    "no record" is a distinct answer from every other one and is stated as such:
    it means the orchestrator could not read this update's outcome, not that the
    update produced none.

    A refusal also gets its next step (`_DECLINE_NEXT_STEP`). It is the one outcome
    whose host is *fine* — so the report has to say what to act on, or the operator
    reads "still serving its old code" and goes looking for damage there is none of.
    """
    if outcome is None:
        return "no updater record for this update"
    if outcome.kind == "declined":
        reason = f" ({outcome.detail})" if outcome.detail else ""
        return (
            f"DECLINED by its own preflight — nothing was stopped and this host is still "
            f"serving its old code{reason}; {_DECLINE_NEXT_STEP}"
            f"{_stages_clause(outcome)}"
        )
    if outcome.kind == "exited":
        declined = outcome.rc == RESTART_DECLINED_EXIT_CODE
        note = (
            f" (the restart declined; host still serving — {_DECLINE_NEXT_STEP})"
            if declined
            else ""
        )
        reason = f" — {outcome.detail}" if outcome.detail else ""
        return f"updater exited rc={outcome.rc}{note}{reason}{_stages_clause(outcome)}"
    reason = f" — {outcome.detail}" if outcome.detail else ""
    return (
        f"updater left no exit verdict in {outcome.log} — it died mid-flight, before any "
        f"branch of its own chain could report one{reason}{_stages_clause(outcome)}"
    )


def stage_evidence_stuck() -> bool:
    """Whether this host's own stage markers prove its updater is stuck (P1,
    2026-08-30).

    The no-progress fact a live updater lease cannot speak for: the lease is one
    write at the run's start, so a host hung inside a single stage (a stalled `uv`
    download on the Windows runner) reads "still working" for the whole bound. The
    markers are the progress evidence — the tail's last `t=` marker names the stage
    the updater is inside right now, and its age (`current_stage_s`, computed
    against this host's monotonic clock at read time) growing past
    `STAGE_NO_PROGRESS_TIMEOUT_S` means no stage has completed for longer than any
    stage has ever legitimately taken. Same bound, same evidence as the Phase-B
    poll's POLL_NO_PROGRESS verdict. False on every unreadable reading: missing
    evidence is never a kill.
    """
    try:
        outcome = last_updater_outcome()
    except Exception:
        return False
    if outcome is None or outcome.current_stage is None or outcome.current_stage_s is None:
        return False
    return outcome.current_stage_s > STAGE_NO_PROGRESS_TIMEOUT_S


def updater_outcome_terminal(outcome: UpdaterOutcome | None) -> bool:
    """Whether this outcome proves the updater **finished** — the second stop-proof.

    Asked by Phase B beside the updater lease, because the lease alone answers
    "is it still working" only while it is written correctly, and its failure mode
    is the expensive direction: a lease is armed for `UPDATER_LEASE_TTL_S` (the
    family's whole no-progress bound) in one write at the run's start, so anything
    that stops the run from clearing it — a chain that exits before its clear step,
    a clear that cannot reach the DB through the very restart it is part of, a
    killed process — leaves the host reading "still working" for exactly as long as
    the poll is willing to wait. Every one of those is a host that has *provably*
    stopped, sitting out the full bound that exists for hosts that have not.

    A verdict in the log is the independent proof, and it is sound in the one
    direction that matters: `last_updater_outcome` is anchored to this update's
    pause and (on the shared Windows log) to this run's own start marker, so a
    verdict that reads back at all was written by this run — and it is written at
    the end of a branch, never before one.
    """
    return outcome is not None and outcome.kind in ("exited", "declined")
