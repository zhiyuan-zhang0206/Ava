"""Cluster health probe — `ava cluster health-probe`.

A CLI command designed to run from an OS cron job (launchd on macOS, crontab on
Linux). Assesses cluster health across several signals and exits 0 (healthy) or
1 (unhealthy). With `--auto-rollback`, the probe itself gates rollback on
consecutive failures (tracked in a counter file under $AVA_HOME) to avoid false
positives from transient issues — so both platforms just run the probe with the
same flags, with no shell-side counting.

Checks (all must pass for exit 0):
1. Gateway HTTP liveness — a health endpoint reachable and returning 200.
2. Agent population — at least a configurable minimum of agents in running/idling
   status.
3. Crash-loop detection — no agent has restarted more than N times in the last T
   minutes.
4. Schema health — the applied schema version matches the required version
   (existing `check_schema_version` invariant).
5. Per-service health — every service this host's roles should be running
   answers its probe, and for every endpoint that can prove it, answers *as this
   unit* (`cli.commands._probe`: identity where the endpoint carries one, plain
   liveness where it cannot). A daemon of another cluster holding this unit's
   port is the condition this check exists to make visible; it used to satisfy
   the check with a bare 2xx. The fleet UI entry port (`_gate_probe`) and the
   authenticated private-network Redis relay (`_redis_bridge_probe`) ride here
   too — neither is a service session, and process liveness cannot certify either
   serving path. Alert-only: a failure exits 1 but never
   feeds the auto-rollback counter — a dead frontend session is an outage worth
   waking the owner for, but not proof the cluster code is bad, and a rollback
   would not necessarily fix it (checks 1-4 are the rollback-gating signals).
6. Data-volume usage — the data volume over the watermark (default 90%)
   fails the probe and alerts. Alert-only, same reasoning as check 5: a full
   disk is the 2026-08-08 outage class, but rollback frees no disk space.
7. Editable-install records — the prod venv's `_editable_impl_ava.pth` pointer
   and its `direct_url.json` record name only allowlisted source (read-only
   detection; repair belongs to the converge guard). Alert-only, same reasoning
   as checks 5 and 6: a poisoned pointer is the 2026-08-27 outage class, but
   rollback does not fix a venv record.
8. Source-tree integrity — the prod source checkout reports tamper findings
   (tracked files changed, untracked files outside the runtime-artifact
   whitelist, HEAD moved off the installed commit). Alert-only, same class as
   checks 5-7: an edited tree is the 2026-08-28 outage class (it broke
   `import ava` for every agent), but rollback does not undo an on-disk edit —
   the converge guard resets the tree on the next start/update.

**A failure a running deploy explains does not advance the auto-rollback counter, and
resets it** (`_deploy_suppression`). The same live lease is an expected transition
window for alerts: the episode start is still persisted, but grading pauses while the
lease is live and resumes from the true start if the outage survives the rollout.
Unreadable deploy context explains nothing. Disk pressure is never explained by a
deploy.

Every check failure is tracked in a state file under $AVA_HOME and graded by elapsed
episode time: silent during normal recovery, WARNING after three minutes, ERROR after
ten. The later recovery resolves only episodes that actually fired. The alert is
ingested into the alerts store (source='health-probe')
through the gateway's /api/alerts endpoint — the same pipeline
Grafana alert rules use — which both records the row for the UI and
fans the notification out to the owner's IM channels (im_bridge daemon, the
only sanctioned Telegram surface, see
docs/decisions/2026-08-03-telegram-skill-removed.md). One health alert =
one alerts row + one IM notification.

Every alert is stamped with the cluster name (`[<cluster>] ...`) so the owner
can tell which cluster is talking — a preview cluster's alert must not read
like a prod incident. If the gateway is unreachable (the alert's most
important case — a dead gateway is itself a check failure), the probe falls
back to writing the row and sending the IM itself, using the same ingest
logic; only when the database is down too does it degrade to the legacy
direct-IM path. Without any of this the probe's exit code only lands in the
cron log, which nobody watches — the 2026-07-13 frontend outage ran unalerted
until the owner hit the dead page.

See future/infra/self-evolution-rollback.md for the full design.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Alert machinery (failure counter, edge alerts, auto-rollback gate) lives in
# `_health_alerts` (split out 2026-08-07 to stay under the 800-line ceiling).
# The probe runner uses the pieces below; the rest are re-exported so tests
# and callers that address them as `_cluster_health.<name>` keep working.
from cli.commands._health_alerts import (
    ALERT_STATE_FILE,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    FAILURE_COUNT_FILE,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _alert_failure,
    _alert_recovery,
    _alert_summary,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _deploy_suppression,
    _handle_consecutive_failure,
    _increment_failure_count,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _ingest_alert,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _ingest_alert_fallback,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _notify_owner,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via _cluster_health)
    _reset_failure_count,
)
from shared.loki_index_labels import LokiReadEra, event_stream_selector, split_index_label_window

# Default thresholds. Overridable via CLI flags; the cron wrapper's
# defaults are set at registration time.
DEFAULT_AGENT_MIN = 1
DEFAULT_CRASH_LOOP_MAX_RESTARTS = 5
DEFAULT_CRASH_LOOP_WINDOW_MINUTES = 10
DEFAULT_CONSECUTIVE_THRESHOLD = 3
_LIVENESS_ATTEMPTS = 3
_LIVENESS_RETRY_INTERVAL_S = 30.0
PENDING_LKG_PASSES = 2
PENDING_LKG_MIN_AGE_S = 600.0
PENDING_LKG_PASSES_FILE = "health_probe_pending_lkg_passes"

# Data-volume used fraction at which the probe fails (alert-only, see
# `_disk_usage_failure`). Same line the 312 resource watcher alerts at
# (90% CRITICAL) and the 2026-08-08 incident crossed (92.4%): the disk the
# checkpoint/trace mirrors grow on filling is the gateway-can't-start outage
# class, so the owner should hear before trace auto-degrade at 0.9 kicks in.
DEFAULT_DISK_USAGE_WATERMARK = 0.90


def _gateway_liveness() -> bool:
    """Check that the gateway's health endpoint returns HTTP 200.

    Uses the gateway's own `/api/health` (or equivalent). On a single-box
    host, this is `http://localhost:<port>/api/health`; the URL is resolved
    from Settings."""
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_api_base

    base = gateway_api_base()
    url = f"{base}/api/health"
    try:
        resp = dial_get(url, timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


def _gateway_liveness_with_retry() -> bool:
    """Filter a short gateway/data-plane restart window before declaring failure."""
    for attempt in range(_LIVENESS_ATTEMPTS):
        if _gateway_liveness():
            return True
        if attempt < _LIVENESS_ATTEMPTS - 1:
            time.sleep(_LIVENESS_RETRY_INTERVAL_S)
    return False


def _data_plane_abnormal() -> bool:
    """True when either dependency behind the gateway is currently unreachable."""
    import shared.db
    from shared.redis_client import sync_redis

    try:
        with shared.db.connect(autocommit=True):
            pass
    except Exception:
        return True
    try:
        client = sync_redis()
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType] — redis-py types ping's optional argument as Unknown.
        finally:
            client.close()
    except Exception:
        return True
    return False


def _agent_population(min_agents: int) -> bool:
    """Check that at least `min_agents` agents are in running/idling status.

    Queries the central DB directly — the probe runs on the gateway machine
    and has DB access. A cluster with zero live agents is effectively dead."""
    import shared.db

    try:
        with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status IN ('running', 'idling') "
                "AND lease_expires_at > now()"
            )
            row = cur.fetchone()
            if row is None:
                return False
            count: int = row[0]
            return count >= min_agents
    except Exception:
        # DB unreachable — if the gateway is up but DB is down, that is itself
        # an unhealthy state. The gateway liveness check would also fail in
        # that case, so this is a secondary signal.
        return False


def _agent_population_failure_class(min_agents: int) -> str | None:
    """Classify a failed population check without turning DB loss into code evidence."""
    import shared.db

    try:
        with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status IN ('running', 'idling') "
                "AND lease_expires_at > now()"
            )
            row = cur.fetchone()
    except Exception:
        return "environment"
    if row is None:
        return "code"
    return "code" if row[0] < min_agents else None


def _reset_pending_lkg_streak(home: Path) -> None:
    """Forget a partial observation window after any rollback-gating failure."""
    (home / PENDING_LKG_PASSES_FILE).unlink(missing_ok=True)


def _advance_pending_lkg(home: Path) -> None:
    """Record a healthy gating pass and promote a mature pending LKG candidate."""
    from shared.cluster_pin import get_pending_known_good, promote_pending_known_good_if_ready

    marker = home / PENDING_LKG_PASSES_FILE
    try:
        pending = get_pending_known_good()
        if pending is None:
            marker.unlink(missing_ok=True)
            return
        pending_sha, _pending_at = pending
        try:
            stored_sha, stored_count = marker.read_text().splitlines()
            count = int(stored_count) + 1 if stored_sha == pending_sha else 1
        except (FileNotFoundError, ValueError):
            count = 1
        marker.write_text(f"{pending_sha}\n{count}")
        if count >= PENDING_LKG_PASSES and promote_pending_known_good_if_ready(
            min_age_s=PENDING_LKG_MIN_AGE_S
        ):
            print(f"  ✓ last-known-good advanced -> {pending_sha[:7]} (observation window passed)")
            marker.unlink(missing_ok=True)
    except Exception as exc:
        print(
            f"  . pending last-known-good observation unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )


def _unhealthy(
    home: Path,
    message: str,
    *,
    auto_rollback: bool,
    threshold: int,
    failure_class: str = "code",
) -> int:
    """Alert a gating failure, counting only code/config evidence toward rollback."""
    print(message, file=sys.stderr)
    _reset_pending_lkg_streak(home)
    deploying = _deploy_suppression()
    _alert_failure(home, message, deploy_explains=deploying is not None)
    if deploying is None:
        if failure_class == "environment":
            print("  environment-class failure — NOT counted toward auto-rollback", file=sys.stderr)
        elif auto_rollback:
            _handle_consecutive_failure(
                home, threshold, failure_class=failure_class, reason=message
            )
        return 1

    print(f"  deploy in flight — alert grading paused ({deploying})", file=sys.stderr)
    _reset_failure_count(home)
    print(
        f"  deploy in flight — NOT counting this failure toward auto-rollback "
        f"(counter reset; the pre-deploy failures were about the old commit): {deploying}",
        file=sys.stderr,
    )
    return 1


def _crash_loop_detection(max_restarts: int, window_minutes: int) -> bool:
    """Check that no agent has restarted more than `max_restarts` times in
    the last `window_minutes` minutes. Returns True if healthy (no crash loop
    detected), False if any agent exceeds the threshold.

    A crash-looping agent is one whose process dies and is resurrected
    repeatedly in a short window — the signature of a bad prompt / skill /
    code change that the update flow did not catch.

    Reads the Loki event stream directly (task #1197): the PG events copy is
    a read-only archive, so the audit `resurrect` events come from Loki. The
    CLI never imports gateway code (layering) — this is a straight instant
    LogQL query."""
    import httpx

    from shared.config import settings

    end = datetime.now(UTC)
    start = end - timedelta(minutes=window_minutes)
    try:
        url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query"
        restarts: dict[str, int] = {}
        slices = split_index_label_window(start, end)
        for slice_ in slices:
            duration_s = max(1, int((slice_.end - slice_.start).total_seconds()))
            selector = event_stream_selector(
                era=slice_.era,
                agent_id=None,
                event_names=["resurrect"],
                indexed_labeled=len(slices) == 2 and slice_.era is LokiReadEra.INDEXED,
            )
            # Instant evaluation at each slice end counts (start, end], so the
            # final slice covers the current crash-loop window (now-w, now].
            logql = (
                f"sum by (agent_id) (count_over_time({selector} "
                f'| event_name="resurrect" | category="audit"[{duration_s}s]))'
            )
            resp = httpx.get(
                url, params={"query": logql, "time": slice_.end.timestamp()}, timeout=30
            )
            resp.raise_for_status()
            for series in resp.json().get("data", {}).get("result", []):
                value = series.get("value")
                if value is None:
                    continue
                agent_id = str(series.get("metric", {}).get("agent_id", ""))
                restarts[agent_id] = restarts.get(agent_id, 0) + int(value[1])
        # Any agent at or over the threshold is a crash loop.
        return not any(count > max_restarts for count in restarts.values())
    except Exception:
        # Loki unreachable — can't check crash loops. Return True (healthy) to
        # avoid a false positive on this secondary signal; the gateway liveness
        # and agent population checks are the primary signals.
        return True


def _schema_health() -> bool:
    """Check that the applied schema version matches the required version.

    Reuses the existing `check_schema_version` invariant that every daemon
    runs at start. A schema/code skew (CodeBehindSchema — code lacks applied
    migrations — or SchemaVersionMismatch — DB lacks code's migrations) is
    unhealthy. A DB that cannot be reached is NOT treated as a schema failure:
    like the crash-loop check's DB-unreachable branch, the connection is the
    precondition for reading the applied set, and a transient pgbouncer blip
    must not fire a false schema alert while code and DB are actually in sync
    (2026-08-03: probe alerted "applied version behind required" on a
    connection error during a pgbouncer flake)."""
    from shared.migrations import CodeBehindSchema, SchemaVersionMismatch, check_schema_version

    try:
        # check_schema_version expects a connection; connect+check inline
        import shared.db

        with shared.db.connect(autocommit=True) as conn:
            check_schema_version(conn)
        return True
    except (CodeBehindSchema, SchemaVersionMismatch):
        # A real skew between the applied set and this checkout's migrations.
        return False
    except Exception:
        # DB unreachable / connection flake — cannot read the applied set.
        # Healthy by default; the gateway-liveness and agent-population checks
        # are the primary signals and keep failing while the DB is truly down.
        return True


def _service_probes() -> list[str]:
    """Probe every service this host's roles should be running; return the failing
    services, each annotated with why (empty = all responding).

    Reuses the `ava status` roster + probe primitives: the role-annotated spec
    list (gated-out services — no display, no bot token — are absent by design
    and skipped) and the per-spec probe (identity / HTTP / TCP / pidfile; a
    probe-less spec reports None and is skipped). Role not resolvable (setup
    unfinished) probes nothing — checks 1-4 are the fallback signals there.

    The probe's `detail` rides along into the entry because this list becomes the
    owner's alert text, and that alert is the only thing a human sees. "ava-ops
    not responding" and "ava-ops is answering, but it is /home/ava/.ava" are the
    same bare session name and completely different incidents — the second one
    means another unit holds this unit's port and no amount of waiting fixes it."""
    import cli.commands as _ns

    roles = _ns._roles_or_none()
    if roles is None:
        return []
    failing: list[str] = []
    for spec, gate_reason in _ns._services_for_roles_annotated(roles):
        if gate_reason is not None:
            continue
        probe = _ns._probe_service(spec)
        if probe.alive is False:
            failing.append(f"{spec.session} ({probe.detail})" if probe.detail else spec.session)
    return failing


def _gate_probe() -> str | None:
    """The fleet UI entry port as an alert signal — the failure text, or None.

    Gateway-capable hosts only (a runner owns no entry port). Reported through
    check 5, which is **alert-only and never feeds the auto-rollback counter** —
    and that placement is the point, not an accident of where it was easiest to
    add. A dark entry port is an outage worth waking the owner for and is exactly
    NOT evidence that the cluster's code is bad: on 2026-08-01 the cause was a
    converge step that booted the gate's launchd job out and failed to load it
    back, which rolling the cluster to the previous commit would re-run
    identically. No rollback puts a supervisor's job back.

    Both halves are alerted on. An unsupervised gate is still serving *now*, so it
    reads healthy to a user, but nothing will restart it — and with the converge
    step no longer touching an unchanged job, the bootout window that could make
    this a false positive is both rare (only a real plist change opens one) and an
    order of magnitude shorter than the probe's interval.
    """
    import cli.commands as _ns

    roles = _ns._roles_or_none()
    if roles is None or "gateway" not in roles:
        return None
    from cli.commands._converge_gate import probe_gate

    status = probe_gate()
    if not status.serving:
        return f"gate entry :{status.entry_port} not answering (the fleet UI is dark)"
    if not status.supervised:
        return (
            f"gate entry :{status.entry_port} answering but unsupervised "
            f"({status.supervisor} is absent — nothing will restart it)"
        )
    return None


def _redis_bridge_probe() -> str | None:
    """End-to-end Redis relay failure text, or None when healthy/not required.

    Gateway-only and alert-only, like the gate: a failed host listener is an
    infrastructure outage, not evidence that rolling application code back is
    safe or useful.  The probe authenticates and PINGs through the off-box
    endpoint; a loaded launchd label or open TCP port alone cannot certify the
    forwarding path.
    """
    import cli.commands as _ns

    roles = _ns._roles_or_none()
    if roles is None or "gateway" not in roles:
        return None
    from cli.commands._converge_redis_bridge import probe_redis_bridge

    status = probe_redis_bridge()
    if not status.required:
        return None
    if not status.serving:
        endpoint = f" {status.endpoint}" if status.endpoint else ""
        return f"Redis bridge{endpoint} failed authenticated PING ({status.detail})"
    if not status.supervised:
        return (
            f"Redis bridge {status.endpoint} serves PING but is unsupervised "
            "(launchd job com.ava.redis-bridge is absent)"
        )
    return None


def _disk_usage_fraction() -> float | None:
    """Used fraction of the data volume, df-style, or None when unmeasurable.

    Uses the df(1) numbers (used / (used + free)) — the same Capacity column
    operators read — not shutil.disk_usage: on macOS APFS, statvfs on the data
    volume reports the whole container (sealed system volume and APFS
    snapshots included), which reads ~3 points higher than df and would fire
    the watermark early. Linux falls back to ``/``. None when df is missing or
    its output is unparsable — a disk-full alarm must never be synthesized
    from a broken measurement.
    """
    volume = "/System/Volumes/Data" if Path("/System/Volumes/Data").is_dir() else "/"
    try:
        out = subprocess.run(
            ["df", "-Pk", volume],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        fields = out.strip().splitlines()[-1].split()
        used, avail = int(fields[2]), int(fields[3])
        return used / (used + avail)
    except (ValueError, IndexError):
        return None


def _disk_usage_failure(watermark: float = DEFAULT_DISK_USAGE_WATERMARK) -> str | None:
    """Alert text when the data volume is over the watermark, else None.

    Reported through check 6, which is **alert-only and never feeds the
    auto-rollback counter** — the same placement logic as check 5: a full disk
    is the 2026-08-08 outage class (checkpoint growth filled the disk and the
    gateway could not start), but rolling the cluster back to a previous
    commit frees no disk space, so it is not rollback evidence. The edge
    alert machinery (state file) keeps a persistent over-watermark condition
    to one firing + one recovery notification, matching the R7
    chronic-condition semantics in the Grafana rules.
    """
    fraction = _disk_usage_fraction()
    if fraction is None or fraction <= watermark:
        return None
    return f"data volume {fraction:.1%} used (watermark {watermark:.0%})"


def _editable_install_failure() -> str | None:
    """Alert text when the prod venv's editable-install records name source
    outside the allowlist, else None.

    Reported through check 7, which is **alert-only and never feeds the
    auto-rollback counter** — the same placement logic as checks 5 and 6: a
    poisoned pointer is the 2026-08-27 outage class (a worktree ``uv sync``
    under a polluted ``VIRTUAL_ENV`` silently repointed the prod venv at
    disposable source), but rolling the cluster back to a previous commit
    does not fix a venv record — the converge guard repairs it on the next
    ``ava start`` / ``ava cluster update`` (``_ensure_prod_editable_pth``).
    The probe only detects and never writes, so it also covers the read-only
    emergency mode where the guard's repair is skipped.

    Delegates to ``shared.editable_install.editable_install_violations`` —
    the public read-only twin of the converge guard's repair primitives —
    with the same exact-root allowlist (prod source plus the ``~/Ava`` dev
    clone), so the detector and the fixer can never disagree about what is
    poisoned.
    """
    import shared.cluster_drift
    import shared.editable_install as ei

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return None
    violations = list(
        ei.editable_install_violations(source_root, allowed_roots=(Path.home() / "Ava",))
    )
    violations.extend(ei.editable_console_script_violations(source_root))
    if not violations:
        return None
    return (
        "prod venv editable install names non-allowlisted source or is inconsistent: "
        + "; ".join(violations)
    )


def _source_tree_failure() -> str | None:
    """Alert text when the prod source checkout is tampered with, else None.

    Reported through check 8, which is **alert-only and never feeds the
    auto-rollback counter** — the same placement logic as checks 5-7: a
    tampered tree is the 2026-08-28 outage class (edited source broke
    ``import ava`` for every agent on the box), but rolling the cluster back
    does not undo an on-disk edit — the converge guard resets the tree on the
    next ``ava start`` / ``ava cluster update`` ("source tree reset + clean").
    The probe only detects and never writes, so it also covers the read-only
    emergency mode where the guard's repair is skipped.

    Delegates to ``shared.source_tree_guard.source_tree_violations`` — the
    public read-only twin of the converge guard's repair primitive — with the
    same whitelist, so the detector and the fixer can never disagree about
    what is legal.

    A checkout the guard could NOT evaluate (not a git checkout, or a git
    command failed) yields a distinct "guard skipped" alert instead of a
    clean pass: a broken git is the state in which tampering becomes
    invisible, so the probe must name the guard itself as failing.
    """
    import shared.cluster_drift
    import shared.source_tree_guard as stg

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return None
    violations = stg.source_tree_violations(source_root)
    if not violations:
        return None
    if any(v.startswith(stg.GUARD_SKIPPED_PREFIX) for v in violations):
        detail = "; ".join(v.removeprefix(stg.GUARD_SKIPPED_PREFIX).strip() for v in violations)
        return f"prod source tree guard skipped: {detail}"
    return "prod source tree tampered: " + "; ".join(violations)


def _run_source_tree_check(home: Path) -> int | None:
    """Run check 8 — alert-only, so it bypasses ``_unhealthy`` (a tampered
    tree is the 2026-08-28 outage class, but rollback does not undo an on-disk
    edit). Returns None when the check passes, 1 when it alerts."""
    failure = _source_tree_failure()
    if failure is None:
        return None
    message = f"FAIL: source tree — {failure}"
    print(message, file=sys.stderr)
    _alert_failure(home, message, deploy_explains=_deploy_suppression() is not None)
    return 1


def run_health_probe(
    *,
    agent_min: int | None = None,
    crash_loop_max_restarts: int = DEFAULT_CRASH_LOOP_MAX_RESTARTS,
    crash_loop_window_minutes: int = DEFAULT_CRASH_LOOP_WINDOW_MINUTES,
    check_crash_loops: bool = True,
    check_schema: bool = True,
    auto_rollback: bool = False,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
) -> int:
    """Run all health checks. Returns 0 if healthy, 1 if any check fails.

    Each check is run independently; the first failure short-circuits with a
    diagnostic message to stderr. With `auto_rollback`, the probe tracks
    consecutive failures in a counter file under $AVA_HOME and triggers
    `ava cluster rollback` once they reach `threshold` (a passing run resets
    the counter). The per-service check (5) fails the probe but never feeds
    that counter. Every failure and the eventual recovery also pushes an
    edge-triggered owner alert (see `_alert_failure` / `_alert_recovery`).
    This is the entry point for both the CLI command and the OS cron job."""
    from shared.paths import ava_home, prod_service_checkout_error, repo_root

    home = ava_home()
    refusal = prod_service_checkout_error(repo_root())
    if refusal is not None:
        # A worktree/dev checkout driving the probe is the 2026-08-07 accident
        # (Task #1025): worktree code misjudges schema health against prod data
        # and auto-rolls-back the cluster. Refuse with a distinct exit code (2)
        # so the cron log shows the refusal; never count it toward the
        # auto-rollback counter.
        print(f"health-probe refused: {refusal}", file=sys.stderr)
        return 2

    # 1. Gateway liveness (primary signal)
    if not _gateway_liveness_with_retry():
        failure_class = "environment" if _data_plane_abnormal() else "code"
        return _unhealthy(
            home,
            "FAIL: gateway liveness — health endpoint unreachable or non-200",
            auto_rollback=auto_rollback,
            threshold=threshold,
            failure_class=failure_class,
        )
    print("  ✓ gateway liveness")

    # 2. Agent population
    # `agent_min` defaults to the cluster's AVA_HEALTH_PROBE_AGENT_MIN (itself
    # 1): a test/QA cluster with no resident agents sets it to 0 or the check
    # fails forever and --auto-rollback cycles the checkout back to the
    # last-known-good commit (2026-08-10 preview incident).
    if agent_min is None:
        from shared.config import settings

        agent_min = settings.daemon.health_probe_agent_min
    if not _agent_population(agent_min):
        return _unhealthy(
            home,
            f"FAIL: agent population — fewer than {agent_min} agent(s) running/idling",
            auto_rollback=auto_rollback,
            threshold=threshold,
            failure_class=_agent_population_failure_class(agent_min) or "code",
        )
    print(f"  ✓ agent population (>= {agent_min})")

    # 3. Crash-loop detection (secondary signal)
    if check_crash_loops:
        if not _crash_loop_detection(crash_loop_max_restarts, crash_loop_window_minutes):
            return _unhealthy(
                home,
                f"FAIL: crash-loop detected — agent(s) restarted > {crash_loop_max_restarts} "
                f"times in {crash_loop_window_minutes} min",
                auto_rollback=auto_rollback,
                threshold=threshold,
            )
        print(
            f"  ✓ crash-loop check (<= {crash_loop_max_restarts} restarts / "
            f"{crash_loop_window_minutes} min)"
        )

    # 4. Schema health
    if check_schema:
        if not _schema_health():
            return _unhealthy(
                home,
                "FAIL: schema health — applied version behind required (CodeBehindSchema)",
                auto_rollback=auto_rollback,
                threshold=threshold,
            )
        print("  ✓ schema health")

    # Checks 1-4 — the rollback-gating set — all passed. Reset the consecutive-
    # failure counter BEFORE the alert-only check 5 runs: a service/host probe
    # failure is not rollback evidence, and a stale count left in place would let
    # non-adjacent gating failures accumulate into an unattended rollback (the
    # same adjacency rule the deploy suppression applies — a run that is healthy
    # in every rollback-gating dimension breaks the run, and resetting costs
    # nothing a cold start would not also cost).
    if auto_rollback:
        _reset_failure_count(home)
    _advance_pending_lkg(home)

    # 5. Per-service health + host-level gate / Redis bridge — alert-only: fails
    # the probe but does NOT feed the auto-rollback counter (see module
    # docstring), so it bypasses _unhealthy and alerts directly.
    failing = _service_probes() + [
        failure for failure in (_gate_probe(), _redis_bridge_probe()) if failure is not None
    ]
    if failing:
        message = f"FAIL: service probe — not healthy: {', '.join(sorted(failing))}"
        print(message, file=sys.stderr)
        _alert_failure(home, message, deploy_explains=_deploy_suppression() is not None)
        return 1
    print("  ✓ service probes")

    # 6. Data-volume usage — alert-only, same class as check 5: a full disk is
    # the 2026-08-08 outage class (checkpoint growth filled the disk and the
    # gateway could not start), but rolling back code frees no disk space, so
    # it bypasses _unhealthy and alerts directly (edge-triggered, one firing +
    # one recovery per episode).
    disk_failure = _disk_usage_failure()
    if disk_failure is not None:
        message = f"FAIL: disk usage — {disk_failure}"
        print(message, file=sys.stderr)
        _alert_failure(home, message, deploy_explains=False)
        return 1
    print("  ✓ disk usage")

    # 7. Editable-install records — alert-only, same class as checks 5 and 6:
    # the 2026-08-27 outage class (a worktree uv sync under a polluted
    # VIRTUAL_ENV repointed the prod venv at disposable source), but rolling
    # back code does not fix a venv record — the converge guard repairs it on
    # the next start/update. The probe detects and alerts; it never writes.
    editable_failure = _editable_install_failure()
    if editable_failure is not None:
        message = f"FAIL: editable install — {editable_failure}"
        print(message, file=sys.stderr)
        _alert_failure(home, message, deploy_explains=_deploy_suppression() is not None)
        return 1
    print("  ✓ editable install records")

    # 8. Source-tree integrity — alert-only, same class as checks 5-7: a
    # tampered prod checkout is the 2026-08-28 outage class (edited source
    # broke `import ava` for every agent on the box), but rolling back code
    # does not undo an on-disk edit — the converge guard resets the tree on
    # the next start/update. The probe detects and alerts; it never writes.
    source_check = _run_source_tree_check(home)
    if source_check is not None:
        return source_check
    print("  ✓ source tree integrity")

    # All checks passed (the counter was already reset once checks 1-4 passed,
    # above the alert-only check 5) — clear the alert edge state.
    _alert_recovery(home)
    return 0


def cmd_health_probe(
    *,
    agent_min: int | None = None,
    crash_loop_max_restarts: int = DEFAULT_CRASH_LOOP_MAX_RESTARTS,
    crash_loop_window_minutes: int = DEFAULT_CRASH_LOOP_WINDOW_MINUTES,
    check_crash_loops: bool = True,
    check_schema: bool = True,
    auto_rollback: bool = False,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
) -> int:
    """CLI entry point for `ava cluster health-probe`.

    Exits 0 if the cluster is healthy; exits 1 if any check fails. Diagnostic
    messages go to stderr so the cron job can capture them separately from
    stdout. With `--auto-rollback`, consecutive failures are counted and a
    cluster rollback is triggered once they reach `--threshold`."""
    return run_health_probe(
        agent_min=agent_min,
        crash_loop_max_restarts=crash_loop_max_restarts,
        crash_loop_window_minutes=crash_loop_window_minutes,
        check_crash_loops=check_crash_loops,
        check_schema=check_schema,
        auto_rollback=auto_rollback,
        threshold=threshold,
    )
