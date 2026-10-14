"""Observe cluster health from the CLI or an OS-scheduled probe.

Checks cover gateway liveness, agent population, schema, selected
services, Redis relay, disk usage, WAL archiving, editable-install records, source integrity,
and provider account health. Any failed check returns unhealthy.

Every check runs on every round — a failing check never stops the others — and
every failed check emits its own `health_probe_failing` event (check, failure
class, message), repeated on every run while it stays failed. Every run also
emits one `health_probe_ran` heartbeat (its absence is the dead-man signal)
carrying the count of checks that failed; Grafana alert rules turn the failing
stream into graded notifications (`for:` is the normal-recovery budget, then
WARNING, then ERROR). The probe itself decides nothing about notifying.

The probe never selects a release, rolls back code, or publishes known-good
state.
"""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import redis.exceptions

from base import telemetry
from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.system.hostport import Host
from cli.commands.cluster._provider_guard import run_provider_guard
from cli.database.runtime import CommandRuntime

# Default thresholds. Overridable via CLI flags; the cron wrapper's
# defaults are set at registration time.
DEFAULT_AGENT_MIN = 1
_LIVENESS_ATTEMPTS = 3
_LIVENESS_RETRY_INTERVAL_S = 30.0

# Data-volume used fraction at which the probe fails (alert-only, see
# `disk_usage_failure`). One line shared across the statvfs family: the
# trace auto-degrade watermark (`AVA_TRACE_DISK_WATERMARK=0.9`), the 312
# resource watcher's CRITICAL threshold, and the 2026-08-08 incident level.
# The disk the checkpoint/trace mirrors grow on filling is the
# gateway-can't-start outage class, so the owner should hear as the trace
# auto-degrade line is reached, not after (metric canon: `disk_usage_fraction`).
DEFAULT_DISK_USAGE_WATERMARK = 0.90


def gateway_liveness() -> bool:
    """Check that the gateway's health endpoint returns HTTP 200.

    Uses the gateway's own `/api/health` (or equivalent). On a single-box
    host, this is `http://localhost:<port>/api/health`; the URL is resolved
    from Settings."""
    from base.cluster.machine import gateway_api_base
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.host.net.http_dial import get as dial_get

    access = gateway_access_from_settings()  # ambient: C
    base = gateway_api_base(access.gateway_url)
    url = f"{base}/api/health"
    try:
        resp = dial_get(url, timeout=10.0)
        return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        # Connection refused / timeout: the gateway is not answering, which is
        # the unhealthy verdict this probe exists to return.
        return False


def gateway_liveness_with_retry() -> bool:
    """Filter a short gateway/data-plane restart window before declaring failure."""
    for attempt in range(_LIVENESS_ATTEMPTS):
        if gateway_liveness():
            return True
        if attempt < _LIVENESS_ATTEMPTS - 1:
            time.sleep(_LIVENESS_RETRY_INTERVAL_S)
    return False


def data_plane_abnormal(*, database_factory: DatabaseFactory) -> bool:
    """True when either dependency behind the gateway is currently unreachable."""
    from base.events.live.bus import EventBus

    try:
        with database_factory().connect(autocommit=True):
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


def agent_population(min_agents: int, *, database_factory: DatabaseFactory) -> bool:
    """Check that at least `min_agents` agents are in running/idling status.

    Queries the central DB directly — the probe runs on the gateway machine
    and has DB access. A cluster with zero live agents is effectively dead."""

    try:
        with database_factory().connect(autocommit=True) as conn, conn.cursor() as cur:
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


def agent_population_failure_class(
    min_agents: int, *, database_factory: DatabaseFactory
) -> str | None:
    """Classify observed low population against DB availability and local intent."""
    from base.deploy.lifecycle import service_selection
    from base.deploy.maintenance import pause_owner

    try:
        with database_factory().connect(autocommit=True) as conn, conn.cursor() as cur:
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


def _report_failing(check: str, message: str, *, failure_class: str = "alert-only") -> None:
    """Emit this run's `health_probe_failing` signal for one failed check.

    `check` is the stable name Grafana groups by; `failure_class` is `code`,
    `environment`, `maintenance`, or `alert-only` (a check whose failure rolling
    code back cannot fix). Emit never raises."""
    telemetry.emit(
        "telemetry",
        "health_probe_failing",
        level="warning",
        source="health-probe",
        attributes={"check": check, "failure_class": failure_class, "message": message},
    )


def _report_ran(unhealthy_checks: int) -> None:
    """Emit this run's `health_probe_ran` heartbeat; its absence means the probe stopped.

    `unhealthy_checks` counts this round's failed checks (0 = healthy)."""
    telemetry.emit(
        "telemetry",
        "health_probe_ran",
        source="health-probe",
        attributes={"unhealthy_checks": unhealthy_checks},
    )


def _unhealthy(check: str, message: str, *, failure_class: str = "code") -> int:
    """Report an unhealthy observation without making a release decision."""
    print(message, file=sys.stderr)
    if failure_class == "maintenance":
        print("  local agent maintenance explains the low population", file=sys.stderr)
    elif failure_class == "environment":
        print("  environment-class failure", file=sys.stderr)
    _report_failing(check, message, failure_class=failure_class)
    return 1


def _alert_only_failure(check: str, message: str) -> int:
    """Report a failed alert-only check (rolling code back does not fix it)."""
    print(message, file=sys.stderr)
    _report_failing(check, message)
    return 1


def schema_health(*, database_factory: DatabaseFactory) -> bool:
    """Check that the applied schema version matches the required version.

    Reuses the existing `check_schema_version` invariant that every daemon
    runs at start. A schema/code skew (CodeBehindSchema — code lacks applied
    migrations — or SchemaVersionMismatch — DB lacks code's migrations) is
    unhealthy. A DB that cannot be reached is NOT treated as a schema failure:
    the connection is the
    precondition for reading the applied set, and a transient pgbouncer blip
    must not fire a false schema alert while code and DB are actually in sync
    (2026-08-03: probe alerted "applied version behind required" on a
    connection error during a pgbouncer flake)."""
    from base.deploy.schema.migrations import (
        CodeBehindSchema,
        SchemaVersionMismatch,
        check_schema_version,
    )

    try:
        # check_schema_version expects a connection; connect+check inline

        with database_factory().connect(autocommit=True) as conn:
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


def service_probes(*, machine: MachineIdentity, host: Host) -> list[str]:
    """Probe every service this host's roles should be running; return the failing
    services, each annotated with why (empty = all responding).

    Reuses the `ava status` roster + probe primitives: the role-annotated spec
    list (gated-out services are intentionally absent and skipped) and the
    ownership-bound per-spec probe. Unknown evidence or an unresolved roster
    cannot certify health, even when the gateway responds.

    The probe's `detail` rides along into the entry because this list becomes the
    `health_probe_failing` message, the only diagnostic a human sees in the alert. "ava-ops
    not responding" and "ava-ops is answering, but it is /home/ava/.ava" are the
    same bare session name and completely different incidents — the second one
    means another unit holds this unit's port and no amount of waiting fixes it."""
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    from base.deploy.lifecycle.service_selection import read_selection
    from services.supervision.roster.spec import services_for_capabilities_annotated

    roles = _repo_commands.roles_or_none(machine=machine)
    if roles is None:
        return ["service roster unavailable"]
    try:
        selection = read_selection()
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        return [f"service selection unavailable ({exc})"]
    failing: list[str] = []
    for spec, gate_reason in services_for_capabilities_annotated(roles, machine=machine, host=host):
        if gate_reason is not None or not selection.enabled(spec.session):
            continue
        probe = _probe_commands.probe_service(spec)
        if probe.alive is not True:
            failing.append(f"{spec.session} ({probe.detail})" if probe.detail else spec.session)
    return failing


def redis_bridge_probe(*, machine: MachineIdentity, config: ConfigBoot) -> str | None:
    """End-to-end Redis relay failure text, or None when healthy/not required.

    Gateway-only and alert-only: a failed host listener is an
    infrastructure outage, not evidence that rolling application code back is
    safe or useful.  The probe authenticates and PINGs through the off-box
    endpoint; an open TCP port alone cannot certify the forwarding path.
    """
    import cli.commands._repo as _repo_commands

    roles = _repo_commands.roles_or_none(machine=machine)
    if roles is None or "gateway" not in roles:
        return None
    from cli.commands.converge.redis_bridge import probe_redis_bridge

    status = probe_redis_bridge(machine=machine, config=config)
    if not status.required:
        return None
    if not status.serving:
        endpoint = f" {status.endpoint}" if status.endpoint else ""
        return f"Redis bridge{endpoint} failed authenticated PING ({status.detail})"
    return None


def disk_usage_fraction() -> float | None:
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


def disk_usage_failure(watermark: float = DEFAULT_DISK_USAGE_WATERMARK) -> str | None:
    """Alert text when the data volume is over the watermark, else None.

    Reported through check 6. A full disk
    is the 2026-08-08 outage class (checkpoint growth filled the disk and the
    gateway could not start), but rolling the cluster back to a previous
    commit frees no disk space, so it is not rollback evidence. The failure is
    reported as `alert-only`, repeated on every run while the volume stays over
    the watermark.
    """
    fraction = disk_usage_fraction()
    if fraction is None or fraction <= watermark:
        return None
    return f"data volume {fraction:.1%} used (watermark {watermark:.0%})"


def walg_archive_failure(*, path_reader: Callable[[], Path | None]) -> str | None:
    """The first broken WAL-archiving condition, or None (also None while WAL-G is off)."""
    from services.backup.walg import probe

    return probe.failure(path_reader=path_reader)


def editable_install_failure() -> str | None:
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


def source_tree_failure() -> str | None:
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


def _run_source_tree_check() -> int | None:
    """Run check 8 — alert-only, so it reports as `alert-only` (a tampered
    tree is the 2026-08-28 outage class, but rollback does not undo an on-disk
    edit). Returns None when the check passes, 1 when it fails."""
    failure = source_tree_failure()
    if failure is None:
        return None
    return _alert_only_failure("source_tree", f"FAIL: source tree — {failure}")


def run_health_probe(
    *,
    agent_min: int | None = None,
    check_schema: bool = True,
    database_factory: DatabaseFactory,
    producer: Callable[[], Any],
    command: CommandRuntime,
) -> int:
    """Return 0 for healthy, 1 for unhealthy, and 2 for a checkout refusal.

    Failed checks emit `health_probe_failing`; nothing here selects, rolls back
    or publishes a release.
    """
    from base.paths import prod_service_checkout_error, repo_root

    refusal = prod_service_checkout_error(repo_root())
    if refusal is not None:
        # A worktree/dev checkout driving the probe is the 2026-08-07 accident
        # (Task #1025): worktree code misjudges schema health against prod data.
        # Refuse the wrong runtime with a distinct exit code (2)
        # so the cron log shows the refusal.
        print(f"health-probe refused: {refusal}", file=sys.stderr)
        return 2

    telemetry.init_telemetry(
        process="cli-health-probe", pipeline=producer(), machine_reader=command.machine.name
    )
    unhealthy_checks = _observe_cluster_health(
        agent_min=agent_min,
        check_schema=check_schema,
        database_factory=database_factory,
        command=command,
    )
    _report_ran(unhealthy_checks=unhealthy_checks)
    return 1 if unhealthy_checks else 0


def _observe_cluster_health(
    *,
    agent_min: int | None,
    check_schema: bool,
    database_factory: DatabaseFactory,
    command: CommandRuntime,
) -> int:
    """Observe one health round, report every failed check, and count them.

    A failing check never stops the round: every check still runs and reports,
    so a schema/code skew cannot silence the disk, service or provider-account
    signals (task #5064). The disk check keeps its position first — a full data
    volume can keep the gateway from starting, and the report must lead with
    that cause; the disk success line keeps check 6's output position below.
    """
    failures = 0

    # Check disk before gateway liveness: a full data volume can keep the
    # gateway from starting, and its alert must name the cause of that outage.
    # A failing reading reports here, first; a healthy one prints in check 6's
    # output position below.
    disk_failure = disk_usage_failure()
    if disk_failure is not None:
        failures += _alert_only_failure("disk_usage", f"FAIL: disk usage — {disk_failure}")

    # 1. Gateway liveness (primary signal)
    if not gateway_liveness_with_retry():
        failure_class = (
            "environment" if data_plane_abnormal(database_factory=database_factory) else "code"
        )
        failures += _unhealthy(
            "gateway_liveness",
            "FAIL: gateway liveness — health endpoint unreachable or non-200",
            failure_class=failure_class,
        )
    else:
        print("  ✓ gateway liveness")

    # 2. Agent population
    # `agent_min` defaults to the cluster's AVA_HEALTH_PROBE_AGENT_MIN (itself
    # 1): a test/QA cluster with no resident agents sets it to 0 or the check
    # otherwise reports a permanent population outage.
    if agent_min is None:
        from base.config import settings

        agent_min = settings.daemon.health_probe_agent_min
    if not agent_population(agent_min, database_factory=database_factory):
        failures += _unhealthy(
            "agent_population",
            f"FAIL: agent population — fewer than {agent_min} agent(s) running/idling",
            failure_class=agent_population_failure_class(
                agent_min, database_factory=database_factory
            )
            or "code",
        )
    else:
        print(f"  ✓ agent population (>= {agent_min})")

    # 3. Schema health
    if check_schema:
        if not schema_health(database_factory=database_factory):
            failures += _unhealthy(
                "schema",
                "FAIL: schema health — applied version behind required (CodeBehindSchema)",
            )
        else:
            print("  ✓ schema health")

    return failures + _check_alert_only_health(
        database_factory=database_factory, command=command, disk_healthy=disk_failure is None
    )


def _check_alert_only_health(
    *, database_factory: DatabaseFactory, command: CommandRuntime, disk_healthy: bool
) -> int:
    """Observe the remaining service, host, and provider health signals: report
    every failure and count them."""
    failures = 0

    # 5. Per-service health and the host-level Redis bridge.
    failing = service_probes(machine=command.machine, host=command.host) + [
        failure
        for failure in (redis_bridge_probe(machine=command.machine, config=command.config),)
        if failure is not None
    ]
    if failing:
        failures += _alert_only_failure(
            "service_probe", f"FAIL: service probe — not healthy: {', '.join(sorted(failing))}"
        )
    else:
        print("  ✓ service probes")

    # 6. Data-volume usage was observed before gateway liveness (see above): a
    # full disk alerts directly instead of being graded as a code failure, and
    # its report led the round. Only the healthy outcome prints here.
    if disk_healthy:
        print("  ✓ disk usage")

    # 6b. WAL archiving — alert-only, same class as checks 5 and 6: a stuck or
    # misconfigured archiver is fixed by repairing the archive path, not by rolling
    # code back. Silent unless AVA_WALG_CONFIG_FILE switches WAL-G on.
    config = command.config

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    archive_failure = walg_archive_failure(path_reader=path_reader)
    if archive_failure is not None:
        failures += _alert_only_failure("walg_archive", f"FAIL: {archive_failure}")
    else:
        from services.backup.walg.config import enabled as walg_enabled

        if walg_enabled(path_reader=path_reader):
            print("  ✓ WAL archiving")

    # 7. Editable-install records — alert-only, same class as checks 5 and 6:
    # the 2026-08-27 outage class (a worktree uv sync under a polluted
    # VIRTUAL_ENV repointed the prod venv at disposable source), but rolling
    # back code does not fix a venv record. Explicit installation repair owns
    # recovery; this probe and ordinary startup never rewrite these records.
    editable_failure = editable_install_failure()
    if editable_failure is not None:
        failures += _alert_only_failure(
            "editable_install", f"FAIL: editable install — {editable_failure}"
        )
    else:
        print("  ✓ editable install records")

    # 8. Source-tree integrity — alert-only, same class as checks 5-7: a
    # tampered checkout that a source-run home executes is the 2026-08-28
    # outage class (edited source broke `import ava` for every agent on the
    # box), but rolling back code does not undo an on-disk edit. The probe
    # detects and alerts; it never writes, and nothing repairs the tree for
    # the operator.
    source_check = _run_source_tree_check()
    if source_check is not None:
        failures += source_check
    else:
        print("  ✓ source tree integrity")

    # 9-10. Provider account guard — alert-only, like checks 5-8 (see `_provider_guard`).
    failures += run_provider_guard(report=_report_failing, database_factory=database_factory)

    return failures


def cmd_health_probe(
    *,
    agent_min: int | None = None,
    check_schema: bool = True,
    database_factory: DatabaseFactory,
    producer: Callable[[], Any],
    command: CommandRuntime,
) -> int:
    """Report cluster health through exit status, diagnostics, and `health_probe_failing` events."""
    return run_health_probe(
        agent_min=agent_min,
        check_schema=check_schema,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )
