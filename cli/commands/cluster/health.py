"""Observe cluster health from the CLI or an OS-scheduled probe.

Checks cover gateway liveness, agent population, crash loops, schema, selected
services, Redis relay, disk usage, WAL archiving, editable-install records, source integrity,
and provider account health. Any failed check returns unhealthy.

Outage episodes retain their first observation and grade by elapsed time:
normal recovery stays quiet, then WARNING escalates to ERROR. A live deploy
pauses explained alert grading; disk pressure remains independent. Recovery
resolves only alerts that fired. Owner notification uses the alerts ingest and
its local fallback when the gateway is unavailable.

The probe never selects a release, rolls back code, or publishes known-good
state.
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import redis.exceptions

# Outage episodes and edge alerts live in
# `health_alerts` (split out 2026-08-07 to stay under the 800-line ceiling).
# The probe runner uses the pieces below; the rest are re-exported so tests
# and callers that address them as `health.<name>` keep working.
from cli.commands.cluster._provider_guard import run_provider_guard
from cli.commands.cluster.health_alerts import (
    ALERT_STATE_FILE,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via health)
    _alert_failure,
    _alert_recovery,
    _alert_summary,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via health)
    _deploy_suppression,
    _ingest_alert,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via health)
    _ingest_alert_fallback,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via health)
    notify_owner,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # re-export (tests access via health)
)

# Default thresholds. Overridable via CLI flags; the cron wrapper's
# defaults are set at registration time.
DEFAULT_AGENT_MIN = 1
DEFAULT_CRASH_LOOP_MAX_RESTARTS = 5
# Crash-loop window: restarts within the last 10 minutes count toward the
# loop (with DEFAULT_CRASH_LOOP_MAX_RESTARTS above); wide enough for a
# flapping daemon to trip, narrow enough that old restarts age out
# (task #3696 exception inventory).
DEFAULT_CRASH_LOOP_WINDOW_MINUTES = 10
_LIVENESS_ATTEMPTS = 3
_LIVENESS_RETRY_INTERVAL_S = 30.0

# Data-volume used fraction at which the probe fails (alert-only, see
# `_disk_usage_failure`). One line shared across the statvfs family: the
# trace auto-degrade watermark (`AVA_TRACE_DISK_WATERMARK=0.9`), the 312
# resource watcher's CRITICAL threshold, and the 2026-08-08 incident level.
# The disk the checkpoint/trace mirrors grow on filling is the
# gateway-can't-start outage class, so the owner should hear as the trace
# auto-degrade line is reached, not after (metric canon: `_disk_usage_fraction`).
DEFAULT_DISK_USAGE_WATERMARK = 0.90


def _gateway_liveness() -> bool:
    """Check that the gateway's health endpoint returns HTTP 200.

    Uses the gateway's own `/api/health` (or equivalent). On a single-box
    host, this is `http://localhost:<port>/api/health`; the URL is resolved
    from Settings."""
    from base.cluster.machine import gateway_api_base
    from base.host.net.http_dial import get as dial_get

    base = gateway_api_base()
    url = f"{base}/api/health"
    try:
        resp = dial_get(url, timeout=10.0)
        return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        # Connection refused / timeout: the gateway is not answering, which is
        # the unhealthy verdict this probe exists to return.
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
    from base.db import Database
    from base.events.live.bus import EventBus

    try:
        with Database.from_settings().connect(autocommit=True):
            pass
    except (psycopg.Error, OSError):
        return True
    try:
        client = EventBus.from_settings().sync_redis()
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType] — redis-py types ping's optional argument as Unknown.
        finally:
            client.close()
    except (redis.exceptions.RedisError, OSError):
        return True
    return False


def _agent_population(min_agents: int) -> bool:
    """Check that at least `min_agents` agents are in running/idling status.

    Queries the central DB directly — the probe runs on the gateway machine
    and has DB access. A cluster with zero live agents is effectively dead."""
    from base.db import Database

    try:
        with Database.from_settings().connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status IN ('running', 'idling') "
                "AND lease_expires_at > now()"
            )
            row = cur.fetchone()
            if row is None:
                return False
            count: int = row[0]
            return count >= min_agents
    except (psycopg.Error, OSError):
        # DB unreachable — if the gateway is up but DB is down, that is itself
        # an unhealthy state. The gateway liveness check would also fail in
        # that case, so this is a secondary signal.
        return False


def _agent_population_failure_class(min_agents: int) -> str | None:
    """Classify observed low population against DB availability and local intent."""
    from base.db import Database
    from base.deploy.lifecycle import service_selection
    from base.deploy.maintenance import pause_owner

    try:
        with Database.from_settings().connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents_meta WHERE status IN ('running', 'idling') "
                "AND lease_expires_at > now()"
            )
            row = cur.fetchone()
    except (psycopg.Error, OSError):
        return "environment"
    if row is None:
        return "code"
    if row[0] >= min_agents:
        return None
    current = pause_owner.read()
    if not service_selection.read_selection().enabled("agent-host") or (
        current.status == "paused" and current.maintenance is not None
    ):
        return "maintenance"
    return "code"


def _grade(home: Path, message: str) -> None:
    """Grade one failed check, annotating the deploy that pauses its grading."""
    deploying = _deploy_suppression()
    _alert_failure(home, message, deploy_explains=deploying is not None)
    if deploying is not None:
        print(f"  deploy in flight — alert grading paused ({deploying})", file=sys.stderr)


def _unhealthy(home: Path, message: str, *, failure_class: str = "code") -> int:
    """Report an unhealthy observation without making a release decision."""
    print(message, file=sys.stderr)
    if failure_class == "maintenance":
        print("  local agent maintenance explains the low population", file=sys.stderr)
    elif failure_class == "environment":
        print("  environment-class failure", file=sys.stderr)
    _grade(home, message)
    return 1


def _crash_loop_detection(max_restarts: int, window_minutes: int) -> bool:
    """Check that no agent has restarted more than `max_restarts` times in
    the last `window_minutes` minutes. Returns True if healthy (no crash loop
    detected), False if any agent exceeds the threshold.

    A crash-looping agent is one whose process dies and is resurrected
    repeatedly in a short window — the signature of a bad prompt / skill /
    code change that the update flow did not catch.

    Counts the audit `resurrect` rows of `audit_events` per agent. The CLI never imports
    gateway code (layering) — this is a straight SQL read."""
    from base.db import Database

    end = datetime.now(UTC)
    start = end - timedelta(minutes=window_minutes)
    try:
        with Database.from_settings().connect(autocommit=True) as conn:
            rows = conn.execute(
                "SELECT agent_id, count(*) FROM audit_events "
                "WHERE event_name = 'resurrect' AND ts > %s AND ts <= %s GROUP BY agent_id",
                (start, end),
            ).fetchall()
        # Any agent over the threshold is a crash loop.
        return not any(count > max_restarts for _agent, count in rows)
    except (psycopg.Error, OSError):
        # Database unreachable — can't check crash loops. Return True (healthy) to
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
    from base.db import Database
    from base.deploy.schema.migrations import (
        CodeBehindSchema,
        SchemaVersionMismatch,
        check_schema_version,
    )

    try:
        # check_schema_version expects a connection; connect+check inline

        with Database.from_settings().connect(autocommit=True) as conn:
            check_schema_version(conn)
        return True
    except (CodeBehindSchema, SchemaVersionMismatch):
        # A real skew between the applied set and this checkout's migrations.
        return False
    except (psycopg.Error, OSError):
        # DB unreachable / connection flake — cannot read the applied set.
        # Healthy by default; the gateway-liveness and agent-population checks
        # are the primary signals and keep failing while the DB is truly down.
        return True


def _service_probes() -> list[str]:
    """Probe every service this host's roles should be running; return the failing
    services, each annotated with why (empty = all responding).

    Reuses the `ava status` roster + probe primitives: the role-annotated spec
    list (gated-out services are intentionally absent and skipped) and the
    ownership-bound per-spec probe. Unknown evidence or an unresolved roster
    cannot certify health, even when the gateway responds.

    The probe's `detail` rides along into the entry because this list becomes the
    owner's alert text, and that alert is the only thing a human sees. "ava-ops
    not responding" and "ava-ops is answering, but it is /home/ava/.ava" are the
    same bare session name and completely different incidents — the second one
    means another unit holds this unit's port and no amount of waiting fixes it."""
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
    from base.deploy.lifecycle.service_selection import read_selection

    roles = _repo_commands._roles_or_none()
    if roles is None:
        return ["service roster unavailable"]
    try:
        selection = read_selection()
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        return [f"service selection unavailable ({exc})"]
    failing: list[str] = []
    for spec, gate_reason in _repo_commands._services_for_roles_annotated(roles):
        if gate_reason is not None or not selection.enabled(spec.session):
            continue
        probe = _probe_commands._probe_service(spec)
        if probe.alive is not True:
            failing.append(f"{spec.session} ({probe.detail})" if probe.detail else spec.session)
    return failing


def _redis_bridge_probe() -> str | None:
    """End-to-end Redis relay failure text, or None when healthy/not required.

    Gateway-only and alert-only: a failed host listener is an
    infrastructure outage, not evidence that rolling application code back is
    safe or useful.  The probe authenticates and PINGs through the off-box
    endpoint; a loaded launchd label or open TCP port alone cannot certify the
    forwarding path.
    """
    import cli.commands._repo as _repo_commands

    roles = _repo_commands._roles_or_none()
    if roles is None or "gateway" not in roles:
        return None
    from cli.commands.converge.redis_bridge import probe_redis_bridge

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
    """Used fraction of the data volume, statvfs-family, or None when unmeasurable.

    Uses ``shutil.disk_usage`` — the same measurement the trace disk-watermark
    guard (``AVA_TRACE_DISK_WATERMARK``, ``base/telemetry/trace_mirror.py``) and the
    macmini resource watcher make, so the probe fires at the line the owner is
    already told about. df(1) was the original measure, but its offset to the
    statvfs family is unstable in both directions (2026-08-24: ~0.8 points
    higher; 2026-09-14: ~2-4 points lower), and a df-calibrated 0.90 fires at
    ≈ statvfs 0.938 — later than the trace auto-degrade this alert exists to
    precede (metric canon 2026-09-14: judge disk waterlines in the statvfs
    family only). Linux falls back to ``/``. None when unmeasurable — a
    disk-full alarm must never be synthesized from a broken measurement.
    """
    volume = "/System/Volumes/Data" if Path("/System/Volumes/Data").is_dir() else "/"
    try:
        usage = shutil.disk_usage(volume)
    except OSError:
        return None
    return usage.used / usage.total


def _disk_usage_failure(watermark: float = DEFAULT_DISK_USAGE_WATERMARK) -> str | None:
    """Alert text when the data volume is over the watermark, else None.

    Reported through check 6. A full disk
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


def _walg_archive_failure() -> str | None:
    """The first broken WAL-archiving condition, or None (also None while WAL-G is off)."""
    from services.gateway_side.walg import probe

    return probe.failure()


def _editable_install_failure() -> str | None:
    """Alert text when the prod venv's editable-install records name source
    outside the allowlist, else None.

    A polluted virtualenv can point at disposable source even when the checkout
    itself is unchanged. This probe reads only; ordinary start and converge do
    not repair editable installs. Recovery requires explicit inspection and
    repair of the identified installation.

    The shared inspection helper applies exact-root allowlisting (production
    source plus the stable ~/Ava clone), never an arbitrary descendant.
    """
    import base.deploy.release.editable_install as ei
    from base.deploy.git import cluster_drift

    source_root = cluster_drift.prod_source_dir()
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
    """Report source tamper or unavailable inspection without repairing files.

    Applies to a home that executes its source checkout. This alert-only check
    cannot authorize rollback: it does not repair arbitrary edits.
    """
    import base.deploy.git.source_tree_guard as stg
    from base.deploy.git import cluster_drift

    source_root = cluster_drift.prod_source_dir()
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
    _grade(home, message)
    return 1


def run_health_probe(
    *,
    agent_min: int | None = None,
    crash_loop_max_restarts: int = DEFAULT_CRASH_LOOP_MAX_RESTARTS,
    crash_loop_window_minutes: int = DEFAULT_CRASH_LOOP_WINDOW_MINUTES,
    check_crash_loops: bool = True,
    check_schema: bool = True,
) -> int:
    """Return 0 for healthy, 1 for unhealthy, and 2 for a checkout refusal.

    Observations feed graded owner alerts; nothing here selects, rolls back or
    publishes a release.
    """
    from base.paths import ava_home, prod_service_checkout_error, repo_root

    home = ava_home()
    refusal = prod_service_checkout_error(repo_root())
    if refusal is not None:
        # A worktree/dev checkout driving the probe is the 2026-08-07 accident
        # (Task #1025): worktree code misjudges schema health against prod data.
        # Refuse the wrong runtime with a distinct exit code (2)
        # so the cron log shows the refusal.
        print(f"health-probe refused: {refusal}", file=sys.stderr)
        return 2

    return _observe_cluster_health(
        home,
        agent_min=agent_min,
        crash_loop_max_restarts=crash_loop_max_restarts,
        crash_loop_window_minutes=crash_loop_window_minutes,
        check_crash_loops=check_crash_loops,
        check_schema=check_schema,
    )


def _observe_cluster_health(
    home: Path,
    *,
    agent_min: int | None,
    crash_loop_max_restarts: int,
    crash_loop_window_minutes: int,
    check_crash_loops: bool,
    check_schema: bool,
) -> int:
    """Observe one health round and report the first failed check."""
    # Check disk before gateway liveness: a full data volume can keep the
    # gateway from starting, and its alert must name the cause of that outage.
    # Keep the existing success line in check 6's output position below.
    disk_failure = _disk_usage_failure()
    if disk_failure is not None:
        message = f"FAIL: disk usage — {disk_failure}"
        print(message, file=sys.stderr)
        _alert_failure(home, message, deploy_explains=False)
        return 1

    # 1. Gateway liveness (primary signal)
    if not _gateway_liveness_with_retry():
        failure_class = "environment" if _data_plane_abnormal() else "code"
        return _unhealthy(
            home,
            "FAIL: gateway liveness — health endpoint unreachable or non-200",
            failure_class=failure_class,
        )
    print("  ✓ gateway liveness")

    # 2. Agent population
    # `agent_min` defaults to the cluster's AVA_HEALTH_PROBE_AGENT_MIN (itself
    # 1): a test/QA cluster with no resident agents sets it to 0 or the check
    # otherwise reports a permanent population outage.
    if agent_min is None:
        from base.config import settings

        agent_min = settings.daemon.health_probe_agent_min
    if not _agent_population(agent_min):
        return _unhealthy(
            home,
            f"FAIL: agent population — fewer than {agent_min} agent(s) running/idling",
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
            )
        print("  ✓ schema health")

    return _check_alert_only_health(home)


def _check_alert_only_health(home: Path) -> int:
    """Observe the remaining service, host, and provider health signals."""

    # 5. Per-service health and the host-level Redis bridge.
    failing = _service_probes() + [
        failure for failure in (_redis_bridge_probe(),) if failure is not None
    ]
    if failing:
        message = f"FAIL: service probe — not healthy: {', '.join(sorted(failing))}"
        print(message, file=sys.stderr)
        _grade(home, message)
        return 1
    print("  ✓ service probes")

    # 6. Data-volume usage passed before gateway liveness. A full disk alerts
    # directly instead of being graded as a code failure.
    print("  ✓ disk usage")

    # 6b. WAL archiving — alert-only, same class as checks 5 and 6: a stuck or
    # misconfigured archiver is fixed by repairing the archive path, not by rolling
    # code back. Silent unless AVA_WALG_CONFIG_FILE switches WAL-G on.
    archive_failure = _walg_archive_failure()
    if archive_failure is not None:
        message = f"FAIL: {archive_failure}"
        print(message, file=sys.stderr)
        _grade(home, message)
        return 1
    from services.gateway_side.walg.config import enabled as walg_enabled

    if walg_enabled():
        print("  ✓ WAL archiving")

    # 7. Editable-install records — alert-only, same class as checks 5 and 6:
    # the 2026-08-27 outage class (a worktree uv sync under a polluted
    # VIRTUAL_ENV repointed the prod venv at disposable source), but rolling
    # back code does not fix a venv record. Explicit installation repair owns
    # recovery; this probe and ordinary startup never rewrite these records.
    editable_failure = _editable_install_failure()
    if editable_failure is not None:
        message = f"FAIL: editable install — {editable_failure}"
        print(message, file=sys.stderr)
        _grade(home, message)
        return 1
    print("  ✓ editable install records")

    # 8. Source-tree integrity — alert-only, same class as checks 5-7: a
    # tampered checkout that a source-run home executes is the 2026-08-28
    # outage class (edited source broke `import ava` for every agent on the
    # box), but rolling back code does not undo an on-disk edit. The probe
    # detects and alerts; it never writes, and nothing repairs the tree for
    # the operator.
    source_check = _run_source_tree_check(home)
    if source_check is not None:
        return source_check
    print("  ✓ source tree integrity")

    # 9-10. Provider account guard — alert-only, like checks 5-8 (see `_provider_guard`).
    if (guard_rc := run_provider_guard(home, alert_failure=_alert_failure)) is not None:
        return guard_rc

    # All checks passed — resolve any alert episode that actually fired.
    _alert_recovery(home)
    return 0


def cmd_health_probe(
    *,
    agent_min: int | None = None,
    crash_loop_max_restarts: int = DEFAULT_CRASH_LOOP_MAX_RESTARTS,
    crash_loop_window_minutes: int = DEFAULT_CRASH_LOOP_WINDOW_MINUTES,
    check_crash_loops: bool = True,
    check_schema: bool = True,
) -> int:
    """Report cluster health through exit status, diagnostics, and graded alerts."""
    return run_health_probe(
        agent_min=agent_min,
        crash_loop_max_restarts=crash_loop_max_restarts,
        crash_loop_window_minutes=crash_loop_window_minutes,
        check_crash_loops=check_crash_loops,
        check_schema=check_schema,
    )
