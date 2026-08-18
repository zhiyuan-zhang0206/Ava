"""Health preflight — warning-only data-plane + checkout self-check on `ava start`.

The port-conflict preflight (#1205) bind-checks the cluster's ports before
anything launches; this step extends it into the start-time cluster-health
checklist (#607): it probes the data plane (Postgres + Redis, exactly the URLs
the runtime dials) and the checkout (HEAD vs the cluster pin, dirty marker).
Every finding is printed and appended to `$AVA_HOME/logs/health_preflight.log`
for after-the-fact inspection; nothing blocks — a start continues on any
warning, the same contract as the port preflight. This is the first line of
defense for the "a cluster died and nobody knew" incident class (paired with
the cross-cluster monitor): the next `ava start` / `ava converge` on the box
leaves a dated trail of what was and was not reachable.

Data plane (`_data_plane_warnings`), role-aware:

- gateway: the plane is this host's own instance, which the start sequence
  brings up right AFTER converge. A fully cold start (neither pg nor redis
  bound) is therefore skipped — warning there would fire on every boot and
  teach operators to ignore the log. Every other state probes: one of them
  up, a foreign occupant, or a listener that does not answer (the hung-daemon
  shape the skip-if-running bring-up would otherwise sail past).
- agent-runner: the plane is remote (the gateway's), so it is probed
  unconditionally — an unreachable plane IS the "preview is down" signal this
  log exists for, and the runner has no later local bring-up to fix it.

Checkout (`_checkout_warnings`), prod-install only: dev worktrees are a
development context by construction (feature-branch HEAD, always-dirty tree),
so their state means nothing against the cluster pin and is skipped. A real
install's checkout must sit on the pinned commit with a clean tree; HEAD/pin
relation comes from `shared.cluster_drift.prod_source_pin_relation` (the same
ancestry read `ava status` uses), and the pin is suppressed while an update is
in flight — a rollout legitimately moves the checkout ahead of a pin written
only when the gateway lands the target (the false-alarm shape status.py
guards).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cli.commands._converge_spec import ConvergeCtx
from cli.commands.status import _update_in_flight
from shared.cluster import _port_free
from shared.cluster_drift import prod_source_pin_relation
from shared.config import settings
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.gitenv import git_env
from shared.port_preflight import expected_cluster_ports
from shared.proc import run_bounded

# Bounded git + network probes: a preflight must never hang a start, and the
# data plane's own keepalive posture (30s idle) is far too slow for a gate that
# runs before anything is launched. Mirrors cluster_drift's `_GIT_TIMEOUT_S`.
_GIT_TIMEOUT_S = 5.0
_PROBE_TIMEOUT_S = 3.0

# Path markers of a dev worktree, matching converge_host's prod-install
# discrimination (`is_prod_install`). A worktree checkout's HEAD/dirty state is
# a development context, never cluster drift.
_WORKTREE_MARKERS = (".claude/worktrees", "/.worktrees/")

# Warning text per non-aligned pin relation, mirroring status.py's _PIN_MARKS.
_PIN_RELATION_WARN = {
    "behind": "behind the cluster pin — this host missed a rollout (run `ava cluster update`)",
    "ahead": "ahead of the cluster pin — the checkout moved past it (stray `git pull`?); "
    "a restart resets to the pin, `ava cluster update` advances the cluster",
    "diverged": "diverged from the cluster pin (run `ava cluster update`)",
    "unknown": "pin commit not present in this checkout (run `ava cluster update`)",
}


def _redact(url: str) -> str:
    """The URL without its userinfo — the probes print endpoints, never secrets."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — urlsplit stripped the [...] brackets
        host = f"[{host}]"
    netloc = host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _short_reason(exc: Exception) -> str:
    """One-line, secret-free failure description for a probe error."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    message = lines[-1] if lines else type(exc).__name__
    return message[:120]


def probe_postgres(url: str, timeout: float = _PROBE_TIMEOUT_S) -> str | None:
    """None when a SELECT 1 round-trip against `url` succeeds; an error string
    otherwise. Bounded by `connect_timeout` so a black-holed peer cannot hang a
    start (the data plane's own fail-fast posture, shortened for the gate)."""
    if url == UNANCHORED_DB_SENTINEL:
        return "no cluster connection facts (unanchored checkout)"
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=int(timeout), autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # a probe reports any failure
        return _short_reason(exc)
    return None


def probe_redis(url: str, timeout: float = _PROBE_TIMEOUT_S) -> str | None:
    """None when a PING round-trip against `url` succeeds; an error string
    otherwise. Bounded the same way as `probe_postgres`."""
    import redis as _redis

    try:
        client = _redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
            url, socket_connect_timeout=timeout, socket_timeout=timeout
        )
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType] — same Unknown **kwargs typing
        finally:
            client.close()
    except Exception as exc:  # a probe reports any failure
        return _short_reason(exc)
    return None


def _is_dev_worktree(repo: Path) -> bool:
    path = str(repo.resolve())
    return any(marker in path for marker in _WORKTREE_MARKERS)


def _git_ro(repo: Path, *args: str) -> str | None:
    """Read-only git command in `repo`, bounded + prompt-proof, returning trimmed
    stdout or None when the checkout is not a git repo / git cannot run."""
    if not (repo / ".git").exists():
        return None
    try:
        result = run_bounded(  # git + fixed path + literal args, no user input
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _cluster_pin() -> str | None:
    """The cluster's pinned commit, or None when no rollout has pinned one or the
    pin cannot be read (DB down — the data-plane probe reports that separately)."""
    from shared.cluster_pin import get_cluster_target_sha

    try:
        return get_cluster_target_sha()
    except Exception:  # a preflight must never fail a start
        return None


def _data_plane_warnings(ctx: ConvergeCtx) -> list[str]:
    """Warning lines for the data plane, or [] when it is reachable / there is
    nothing to probe. See the module docstring for the role-aware skip."""
    if ctx.roles is None:
        return []  # unit not configured yet — no URLs to probe
    try:
        pg_url = settings.data_plane.db_url
        redis_url = settings.data_plane.redis_url
    except Exception:  # a preflight must never fail a start
        return []
    if not pg_url or not redis_url:
        return []

    if "gateway" in ctx.roles:
        # postgres/redis are required ClusterPorts keys — `expected_cluster_ports`
        # always returns them (the legacy block and every record shape carry them).
        ports = expected_cluster_ports(ctx.ava_home)
        pg_port = ports["postgres"]
        redis_port = ports["redis"]
        plane_up_at_all = not _port_free(pg_port) or not _port_free(redis_port)
        if not plane_up_at_all:
            # Cold start: neither pg nor redis is bound, and the start sequence
            # brings the instance up right after converge. Probing now would
            # warn on every boot — skip.
            return []

    out: list[str] = []
    if pg_url != UNANCHORED_DB_SENTINEL:
        err = probe_postgres(pg_url)
        if err is not None:
            out.append(f"postgres {_redact(pg_url)}: {err}")
    err = probe_redis(redis_url)
    if err is not None:
        out.append(f"redis {_redact(redis_url)}: {err}")
    return out


def _checkout_warnings(ctx: ConvergeCtx) -> list[str]:
    """Warning lines for the checkout, or [] when it is a dev worktree, not a git
    repo, or aligned and clean."""
    if _is_dev_worktree(ctx.repo):
        return []
    head = _git_ro(ctx.repo, "rev-parse", "HEAD")
    if head is None:
        return []  # not a git repo / unreadable — nothing to report

    out: list[str] = []
    dirty = _git_ro(ctx.repo, "status", "--porcelain")
    if dirty:
        n = len(dirty.splitlines())
        out.append(
            f"checkout {ctx.repo} is dirty ({n} changed file(s)) — uncommitted "
            "changes on the running host; the next rollout force-discards them"
        )

    pin = _cluster_pin()
    if pin is not None and not _update_in_flight():
        relation = prod_source_pin_relation(pin, head, repo=ctx.repo)
        if relation != "aligned":
            out.append(
                f"checkout HEAD {head[:7]} vs cluster pin {pin[:7]}: {_PIN_RELATION_WARN[relation]}"
            )
    return out


def collect_health_warnings(ctx: ConvergeCtx) -> list[str]:
    """All health-preflight warning lines for this unit, or [] when healthy."""
    return _data_plane_warnings(ctx) + _checkout_warnings(ctx)


def ensure_health_preflight(ctx: ConvergeCtx) -> None:
    """Converge step: warn (never block) on data-plane unreachability + checkout
    drift. Best-effort by contract: a preflight must not fail a start, so any
    exception in the scan prints a notice and the start proceeds."""
    try:
        warnings = collect_health_warnings(ctx)
    except Exception as exc:  # a preflight must never fail a start
        print(f"  · health preflight skipped: {exc}", file=sys.stderr)
        return
    if not warnings:
        return

    print(
        "\n⚠  HEALTH PREFLIGHT — data plane / checkout findings (start continues):",
        file=sys.stderr,
    )
    for line in warnings:
        print(f"    {line}", file=sys.stderr)
    print(
        "    The cluster may come up unhealthy — resolve before the next start, or "
        "check the fleet monitor.",
        file=sys.stderr,
    )
    log_dir = ctx.ava_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with (log_dir / "health_preflight.log").open("a", encoding="utf-8") as f:
        for line in warnings:
            f.write(f"{stamp} {line}\n")
    print(f"  · details appended to {log_dir / 'health_preflight.log'}", file=sys.stderr)
