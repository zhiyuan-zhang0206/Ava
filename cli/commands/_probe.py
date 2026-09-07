"""Service probes, the start path's readiness gate, and status row formatting.

`_probe_service` is the one probe behind every operator-facing surface — the
`ava status` per-service rows, `ava cluster health-probe`'s check 5, and the
start path's readiness gate.

**It asks for identity first, and only falls back to liveness when the endpoint
cannot carry any.** A plain HTTP 2xx was the whole test, and a 2xx from
*something* is not evidence that the something is ours: on 2026-07-24 a
pytest-leaked restarter on prod's default port answered 200 for 98 minutes while
the real restarter was dead, and on 2026-07-26 a WSL2 unit's daemons answered the
Windows unit's health ports through the localhost relay. The watchdog has refused
to believe a bare 2xx since (`shared.daemon_health.probe_daemon`); this surface —
the one a human runs and the one alerts fire from — did not, so the condition the
watchdog was shouting about read **green** here. `ServiceSpec.identity_probe`
carries the check, so both now ask the same question of the same endpoint.

Four signal types remain, in precedence order, and the row says which one it got:

| label | what a ✓ means |
|---|---|
| `identity` | 2xx AND the answering process is this unit's (`ServiceSpec.identity_probe`) |
| `http` | 2xx/3xx only — the endpoint carries no identity (frontend: Next.js) |
| `tcp` | the port accepts a connection (milvus: gRPC, uncurlable) |
| `pid` | this unit's pidfile names a live process (the watchdogs, which serve nothing) |

The bottom three are liveness-only **by property of the endpoint, not by
oversight** — see `ServiceSpec.identity_probe`. Showing the label is what keeps
that visible instead of letting every ✓ look equally strong.

`_wait_for_services_ready` polls those same probes for the roster `ava start` just
launched and *reports* which never passed, so the start's exit code can carry
service readiness instead of only "the start process exited cleanly". Why that
mattered enough to change: a human reads the status snapshot and sees the crosses,
but a program reads `rc`, and every programmatic caller of `ava start` was reading
`rc == 0` as "the services are serving". Prod lost that bet — see
`cli/commands/_gateway_ready.py` for the rollout that stalled on it.

The gate is tiered (`CRITICAL_SERVICE_SESSIONS` vs the 45 s
`NON_CRITICAL_SERVICE_READY_TIMEOUT_S` window): the 2026-08-30 rollout spent
182 s of its 197.5 s start waiting on a pitr-uploader no conclusion depended on.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

from cli.commands._repo import ServiceSpec, session_name
from shared.cluster_drift import prod_source_branch_drift as _detect_prod_source_drift
from shared.cluster_drift import prod_source_head_sha as _prod_source_head_sha
from shared.deploy_timing import NON_CRITICAL_SERVICE_READY_TIMEOUT_S
from shared.proc import process_alive
from shared.resilience import ExponentialBackoff, Policy, http_classifier, retry

__all__ = ["_detect_prod_source_drift", "_prod_source_head_sha"]

# The poll's throttle, as a module-local name rather than a `time.sleep` call.
#
# Tests shorten or forbid this sleep to assert the wait's timing, and the only
# handle a bare `time.sleep(...)` call offers is `time.sleep` itself — patching
# `cli.commands._probe.time.sleep` resolves `time` to the *stdlib module object*
# and disables sleeping for the whole process. That is not a wider version of the
# same effect, it is a different one: every wall-clock-bounded retry loop in the
# product (`shared.session_backend._graceful_kill_session` is `while
# time.monotonic() < deadline: ...; time.sleep(0.5)`) keeps its real deadline and
# loses its only throttle, so it spins at full speed for its full bound. On
# 2026-07-30 that turned one `tests/cli` test into a 26 GB process and took the
# swap out from under the prod box (issue #1001).
#
# Bound at import, so patching this name replaces THIS poll's sleep and nothing
# else. Same intent as the `import cli.commands as _ns` indirection below: a
# named seam per patchable behaviour.
_poll_sleep = time.sleep

logger = logging.getLogger(__name__)


def _pidfile_path(spec: ServiceSpec) -> Path | None:
    return spec.pidfile


def _pid_alive(pidfile: Path) -> tuple[bool, int | None]:
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False, None
    # process_alive, not a raw os.kill(pid, 0): on Windows os.kill maps any signal
    # (incl. 0) to TerminateProcess, so the probe would *kill* the daemon it checks
    # (e.g. `ava status` killing the watchdogs). process_alive routes through
    # psutil.pid_exists on Windows and keeps the signal-0 semantics on POSIX.
    return process_alive(pid), pid


# Probe confirm-retry (R2-D, audit-06 Q2): a transient TCP reset / slow
# response at the probe instant must not read as down and feed alerts /
# auto-rollback decisions — one 1s confirm retry. 4xx stays immediate
# (a misconfigured probe), 429/5xx get the confirm. No Retry-After respect:
# a probe must never sleep for the upstream's backoff.
_PROBE_POLICY = Policy(
    max_attempts=2,
    backoff=ExponentialBackoff(base=1.0, factor=1.0, cap=1.0),
    jitter="none",
    classify=http_classifier,
    respect_retry_after=False,
)


def _curl_ok(url: str) -> bool:
    # httpx (a dependency) instead of shelling out to `curl` — `curl` is not
    # guaranteed on PATH (and the old `-o /dev/null` is POSIX-only). Same intent:
    # a 2xx/3xx HTTP response means the service is up.
    import httpx

    def _get() -> None:
        resp = httpx.get(url, timeout=5.0, follow_redirects=False)
        resp.raise_for_status()

    try:
        retry(_PROBE_POLICY)(_get)
    except httpx.HTTPError as exc:
        logger.warning("probe %s failed: %s", url, exc)
        return False
    return True


def _tcp_alive(port: int) -> bool:
    """TCP connect probe — used by gRPC servers (milvus); curl cannot probe gRPC."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


class ServiceProbe(NamedTuple):
    """One service's probe outcome, as the operator surfaces need it.

    Attributes:
        alive: True / False, or None when there is no probe to run at all
            (`browser-mcp`, whose transport is a Unix socket only its own
            healthcheck dials). None is not "assumed healthy" — it is "never
            observed", and the readiness gate treats it as un-gateable.
        label: which signal answered — `identity` / `http` / `tcp` / `pid` /
            `n/a`. Printed, because a ✓ backed by an identity check and a ✓
            backed by a bare 2xx are not the same claim.
        detail: why, in the endpoint's own words. Populated on failure (an
            identity probe's `DaemonProbe.detail` names the fact that failed —
            "home='/home/ava/.ava' != …" is actionable where "down" is not) and
            left empty when there is nothing to add.
        terminal: whether the endpoint is held by something this unit cannot
            evict (`DaemonProbe.terminal` — another `$AVA_HOME`'s daemon, another
            daemon kind, a non-Ava process). Always False for a liveness-only
            signal, which cannot tell an occupant from an outage. The status rows
            do not branch on it — "not serving for us" is one answer there — but
            `_occupied_health_ports` does, because a start that is about to BIND
            the port has to know whether waiting could help.
    """

    alive: bool | None
    label: str
    detail: str
    terminal: bool = False


def _probe_service(spec: ServiceSpec) -> ServiceProbe:
    """Probe one service, preferring identity over liveness.

    Precedence: the spec's `identity_probe` (which already dialled the endpoint
    and verified the answer is ours), then the liveness-only signals for the
    endpoints that cannot carry identity — curl, TCP connect, pidfile. A spec with
    neither reports `alive=None`.
    """
    # Lookup _curl_ok through the package namespace so tests can monkeypatch
    # `cli.commands._curl_ok` to stub HTTP calls. Same pattern in
    # _print_service_row below.
    import cli.commands as _ns

    if spec.identity_probe is not None:
        # The three built-in probes are total wrappers — every failure mode
        # (timeout, refused connection, malformed body) already comes back as a
        # `DaemonProbe`. A plugin-registered `identity_probe` carries no such
        # guarantee, and this is the function behind `ava status`: the command an
        # operator runs precisely when the unit is already misbehaving. One
        # plugin's unhandled exception must not take the whole status screen with
        # it, and a service whose probe cannot answer is not serving for us.
        #
        # The exception's type and message become the row's detail, so a merely
        # buggy probe still says which probe and why, on the surface being read.
        # Swallowing it into a fixed string would trade the crash for a mystery.
        try:
            probe = spec.identity_probe()
        except Exception as exc:
            detail = f"identity probe raised {type(exc).__name__}: {exc}"
            return ServiceProbe(alive=False, label="identity", detail=detail)
        # `terminal` rides along but never changes what a ROW says: a port held
        # by an occupant is exactly as "not serving for us" as an empty one, and
        # whether a respawn could win is the watchdog's question. It is carried
        # because one caller is neither reporting nor respawning —
        # `_occupied_health_ports` is about to bind that port.
        return ServiceProbe(
            probe.alive, "identity", "" if probe.alive else probe.detail, probe.terminal
        )
    if spec.curl_url is not None:
        ok = _ns._curl_ok(spec.curl_url)
        return ServiceProbe(ok, "http", "" if ok else f"no 2xx/3xx from {spec.curl_url}")
    if spec.tcp_port is not None:
        ok = _tcp_alive(spec.tcp_port)
        return ServiceProbe(ok, "tcp", "" if ok else f"nothing accepting on port {spec.tcp_port}")
    pidfile = _pidfile_path(spec)
    if pidfile is None:
        return ServiceProbe(None, "n/a", "")
    alive, pid = _pid_alive(pidfile)
    if pid is None:
        detail = f"no readable pid in {pidfile}"
    else:
        detail = "" if alive else f"pid {pid} is not running"
    return ServiceProbe(alive=alive, label="pid", detail=detail)


# The services the readiness gate holds the whole bound for — the only ones that
# can fail a start. Everything else gets the short non-critical window
# (`NON_CRITICAL_SERVICE_READY_TIMEOUT_S`). Single source of truth: tests pin
# this exact set, so adding or removing a critical service turns the suite red.
#
# The CTO ruling (Task #2183, C2): critical = a failure cuts user-visible core
# function or the ops safety net. gateway / frontend are the serving surface;
# agent-host runs agent turns; im-bridge is the IM alert channel; the two
# watchdogs are the revive safety net. `frontend` is named here even though
# `_SLOW_TO_SERVE_SESSIONS` keeps it out of the wait entirely.
CRITICAL_SERVICE_SESSIONS = frozenset(
    {
        "gateway",
        "frontend",
        "agent-host",
        "gateway-watchdog",
        "agent-runner-watchdog",
        "im-bridge",
    }
)


# The services whose probe says nothing about a launch that just happened. The
# frontend is the whole list: `npm run build` runs for ~30-60 s before Next.js
# answers, so its probe reads False for most of a perfectly good start.
#
# Both launch-time consumers of the probes have to know this, and for the same
# reason — the readiness wait (gating on the frontend would put a minute on every
# gateway start) and `_launch_sessions`' husk check (a mid-build frontend is
# not a husk, and "relaunching" one restarts the build it is in the middle of).
# Stated once, because two copies of this list would drift and the second copy is
# the one nobody reads.
_SLOW_TO_SERVE_SESSIONS = frozenset({"frontend"})


def _probe_judges_a_fresh_launch(spec: ServiceSpec) -> bool:
    """Whether a False probe means "this service is not up" for a just-launched spec.

    False for a service that legitimately takes longer to serve than a start is
    willing to wait for it (`_SLOW_TO_SERVE_SESSIONS`); its state is reported by the
    status snapshot instead, which is read after the delay has had time to pass.
    """
    return spec.session not in _SLOW_TO_SERVE_SESSIONS


def _husk_session_reason(spec: ServiceSpec) -> str | None:
    """Why a live session for `spec` is not evidence its service runs — or None.

    `ava start` skips a service whose session already exists, which is the right
    idempotence guard asked of the wrong thing: the session *process* can stay
    alive, and a process whose daemon logic died is a session that exists with
    nothing behind it. On
    2026-07-30 an unconfirmed force-kill left exactly that behind, the start printed
    `✓ ava-gateway already running`, and prod had no gateway for a minute until the
    watchdog noticed (issue #1015).

    So the guard asks the probe instead — the same probe `ava status` and the
    readiness wait use. A non-None return is the reason to relaunch, in the
    endpoint's own words, so the operator sees why a session they saw listed
    as alive was torn down. None means "skip is correct": the probe passed, the
    endpoint carries no probe at all (`alive is None`), or the probe cannot judge a
    fresh launch (`_probe_judges_a_fresh_launch`).
    """
    import cli.commands as _ns

    if not _probe_judges_a_fresh_launch(spec):
        return None
    probe = _ns._probe_service(spec)
    if probe.alive is not False:
        return None
    return probe.detail or f"{probe.label} probe reports it down"


class OccupiedPort(NamedTuple):
    """One health port this start was about to bind that someone else answers on.

    `detail` is the probe's own words, which already name the occupant's
    `$AVA_HOME` and the URL — the two facts an operator needs to decide which
    unit should move.
    """

    spec: ServiceSpec
    detail: str


def _binds_a_daemon_health_port(spec: ServiceSpec) -> bool:
    """Whether `spec` binds one of this unit's `AVA_*_HEALTH_PORT` ports.

    Read off the spec's own probe target instead of a second list of daemon
    names: every such service is declared with `curl_url=_hz(<daemon>)`
    (`ops.spec`), built from the same `health_port(<daemon>)` call the daemon
    passes to `start_health_server` — so the URL probed and the port bound cannot
    disagree. The gateway (`/api/health`), the browser (CDP `/json/version`),
    milvus (gRPC) and the frontend fall out by the same rule that keeps the
    others in, and none of them is a port `--health-port-base` moves.
    """
    return spec.curl_url is not None and spec.curl_url.endswith("/healthz")


def _occupied_health_ports(specs: tuple[ServiceSpec, ...]) -> tuple[OccupiedPort, ...]:
    """The health ports among `specs` that another unit already answers on.

    `ava start` probes before it binds because the alternative is worse in both
    directions. A daemon launched onto a taken port dies on `[Errno 48] Address
    already in use` and is respawned every watchdog round forever; a daemon whose
    port is being *relayed* from elsewhere is worse still, because the watchdog's
    probe is answered — by the wrong process — and the operator's `ava start`
    prints a clean roster (issue #977: 402 identity-mismatch lines over one
    afternoon, each ending "manual intervention needed").

    Only a `terminal` verdict counts, and that is the whole discrimination: this
    unit's own daemon answers with its own home+name and is ALIVE, a stray of its
    own home is DOWN, and an empty port is DOWN — so an idempotent restart, a
    crashed daemon, and a cold host all pass. `PORT_TAKEN` is reached only by
    something no respawn of ours can dislodge, which is exactly the condition
    that makes launching pointless rather than merely slow.

    Nothing is remembered: the check is a fresh dial every start, so once the
    occupant leaves, the next start proceeds with no state to clear.

    **Only an occupant that answers HTTP on `/healthz` is detectable.** The
    verdict comes from reading a health payload, so a listener speaking any other
    protocol (a Postgres or redis on the port), one that accepts and stays silent,
    and an HTTP server that 404s the path all read DOWN — indistinguishable from
    an empty port — and the start proceeds into the `EADDRINUSE` this gate exists
    to prevent. The gate narrows the failure it was built for (issue #977's relay,
    which answers) and leaves the generic taken-port case where it already was.
    """
    import cli.commands as _ns

    occupied: list[OccupiedPort] = []
    for spec in specs:
        if not _binds_a_daemon_health_port(spec):
            continue
        probe = _ns._probe_service(spec)
        if probe.terminal:
            occupied.append(OccupiedPort(spec, probe.detail))
    return tuple(occupied)


_READY_POLL_INTERVAL_S = 0.5

# How many consecutive polls must agree that an unready service's session is gone
# before the wait stops waiting on it. One reading is not evidence: the backend may
# not have registered a session the start path spawned moments earlier, so a single
# "session missing" right after launch is a race. Two, one interval apart, costs
# `_READY_POLL_INTERVAL_S` instead of the whole bound. Same rule, for the same
# reason, as `_EARLY_EXIT_CONFIRMATIONS` in `cli.commands._gateway_ready`.
_SESSION_GONE_CONFIRMATIONS = 2


class ReadinessWait(NamedTuple):
    """What the readiness wait found, and how it stopped looking.

    `unready` alone cannot answer the operator's first question, because the two
    exits below mean opposite things about the bound. A wait that spent 180 s on
    live-but-not-serving daemons is a case where waiting longer might have helped;
    a wait that returned in a second because every session was gone never spent the
    bound at all, and raising it would change nothing. Reporting only the specs made
    both print the same sentence, and on 2026-07-30 that sentence sent the first
    diagnosis of a failed rollout looking for a too-tight timeout on a wait that had
    taken about a second (#1016).

    `sessions_gone` is the early exit, so it is a fact about *every* spec in
    `unready`; the deadline exit implies at least one of them was still alive (the
    early exit requires all of them to be gone). Both exits concern the CRITICAL
    roster only: `non_critical_unready` carries the demoted services that missed
    their short window — they must be reported and alerted, but they can never
    decide the exit code.
    """

    unready: tuple[ServiceSpec, ...]
    elapsed_s: float
    sessions_gone: bool
    # Defaulted so existing constructions (tests/conftest.py's guard included)
    # keep meaning "nothing non-critical failed".
    non_critical_unready: tuple[ServiceSpec, ...] = ()


def _wait_for_services_ready(specs: tuple[ServiceSpec, ...], timeout_s: float) -> ReadinessWait:
    """Poll each spec's liveness probe until all pass, and report the ones that never do.

    Returns the specs still probing False when the wait stops — an empty `unready`
    means every critical spec passed, which is the signal `ava start` turns into its
    exit code — together with how long that took and which of the two exits ended
    it. A spec counts as ready the moment its probe is no longer False: True, or
    `None` for a probe-less spec, which can never be observed unready and therefore
    can never gate (`browser-mcp` is the one such service on the roster — its
    transport is a Unix socket that only its healthcheck dials).

    The start path calls this just before its status snapshot, and the wait exists
    in the first place because the session spawn returns the instant the process
    starts while a uvicorn daemon needs a beat to bind its port. A healthy roster therefore pays
    nothing beyond the polls it takes to come up; the bound is only ever spent by a
    service that is alive and has bound nothing.

    The roster is tiered by `CRITICAL_SERVICE_SESSIONS`: the critical services get
    the whole `timeout_s` bound and are the only ones that can end the wait
    unready. A non-critical service is waited on only for
    `NON_CRITICAL_SERVICE_READY_TIMEOUT_S`, then leaves the wait whether it is up
    or not — still-unready ones land in `non_critical_unready` for the caller to
    report and alert, never as a failed start (the 2026-08-30 lesson: 182 s of a
    197.5 s local start went to a pitr-uploader whose verdict nothing depended on).

    Two things end the wait early, so the bound is affordable:

    - every remaining critical spec's probe passes (the normal exit, and the only
      one that returns empty — non-critical stragglers ride along in
      `non_critical_unready`);
    - every remaining critical spec's *session* is confirmed gone. A launched
      service whose session died will never bind its port, so waiting cannot help.
      This is the local form of `_gateway_ready`'s `GATEWAY_GONE`, and it is what
      keeps a crashed daemon from costing an operator the full bound before the
      snapshot prints. It requires ALL unready critical specs to be gone: one dead
      `browser` must not cut short the wait of a gateway that is 20 s from serving
      and would then be reported unready when it was merely slow.

    The caller excludes the frontend (a ~30-60 s `npm run build`, which would set the
    floor for every start) and services gated out or `--disable-service`-skipped
    never reach here at all — the roster this receives is `ops.spec`'s capability
    view minus those, so "skipped" and "unready" stay different answers.
    """
    # Go through the package namespace so a test can monkeypatch
    # `cli.commands._probe_service` (same pattern as _probe_service -> _curl_ok).
    import cli.commands as _ns

    started_at = time.monotonic()
    deadline = started_at + timeout_s
    non_critical_deadline = started_at + NON_CRITICAL_SERVICE_READY_TIMEOUT_S
    critical = tuple(s for s in specs if s.session in CRITICAL_SERVICE_SESSIONS)
    non_critical = {s.session: s for s in specs if s.session not in CRITICAL_SERVICE_SESSIONS}
    gone_streak: dict[str, int] = {}
    non_critical_gone_streak: dict[str, int] = {}
    non_critical_unready: list[ServiceSpec] = []
    while True:
        # Non-critical services are waited on only inside their short window:
        # drop the ready ones every poll, drop a session confirmed gone without
        # spending the window, and at the window's end drop whatever is left.
        for name, spec in list(non_critical.items()):
            if _ns._probe_service(spec).alive is not False:
                del non_critical[name]
                continue
            alive = _ns._has_session(session_name(name))
            non_critical_gone_streak[name] = (
                0 if alive else non_critical_gone_streak.get(name, 0) + 1
            )
            if non_critical_gone_streak[name] >= _SESSION_GONE_CONFIRMATIONS:
                del non_critical[name]
                non_critical_unready.append(spec)
        if non_critical and time.monotonic() >= non_critical_deadline:
            non_critical_unready.extend(non_critical.values())
            non_critical.clear()
        # The gate itself is critical-only: the exit code's verdict must not
        # depend on a service the tier demoted. The wait still runs on while a
        # non-critical service has not reached a verdict (ready, confirmed gone,
        # or its window expired) — otherwise a session that dies on the very
        # poll the critical roster clears would be dropped without a report.
        unready = tuple(s for s in critical if _ns._probe_service(s).alive is False)
        if not unready and not non_critical:
            return ReadinessWait(
                (),
                time.monotonic() - started_at,
                sessions_gone=False,
                non_critical_unready=tuple(non_critical_unready),
            )
        if unready:
            for spec in unready:
                alive = _ns._has_session(session_name(spec.session))
                gone_streak[spec.session] = 0 if alive else gone_streak.get(spec.session, 0) + 1
            gone = all(gone_streak[s.session] >= _SESSION_GONE_CONFIRMATIONS for s in unready)
            if gone or time.monotonic() >= deadline:
                return ReadinessWait(
                    unready, time.monotonic() - started_at, gone, tuple(non_critical_unready)
                )
        _poll_sleep(_READY_POLL_INTERVAL_S)


def _print_unready_services(wait: ReadinessWait, timeout_s: float) -> None:
    """Name the services that never became ready, for the operator reading the same
    output as the status snapshot above it.

    The snapshot already shows a cross on each row; this says which crosses are the
    reason for the non-zero exit, so `rc != 0` never arrives without the *what*
    beside it. Printed AFTER the snapshot deliberately — an operator reads down.

    The two exits get two different sentences because they send the reader to two
    different places. Naming the bound is only meaningful when the bound was spent:
    the services are alive and still not serving, so waiting longer might help. When
    every session is gone the wait returned in about a second, the bound is
    irrelevant, and the next question is why the process is absent — printing the
    bound there states an elapsed time the surrounding timestamps contradict, which
    is how the 2026-07-30 rollout post-mortem opened by hunting a timeout that was
    never reached (#1016)."""
    names = ", ".join(session_name(s.session) for s in wait.unready)
    if wait.sessions_gone:
        print(
            f"\n✗ {len(wait.unready)} service(s) not ready after {wait.elapsed_s:.1f}s "
            f"— their sessions are gone: {names}\n"
            f"  They died or were never launched, so the {timeout_s:.0f}s readiness bound was "
            f"not spent and raising it would change nothing;\n"
            f"  read why in $AVA_HOME/logs. The watchdog keeps trying to revive them and "
            f"`ava start` is idempotent to retry.",
            file=sys.stderr,
        )
        return
    print(
        f"\n✗ {len(wait.unready)} service(s) never became ready within {timeout_s:.0f}s "
        f"({wait.elapsed_s:.1f}s elapsed): {names}\n"
        f"  Their sessions are running and still not serving — their rows above show the "
        f"failing probe. Every start step itself succeeded,\n"
        f"  so this host is up but incomplete; the watchdog keeps trying to revive them, "
        f"`ava status` re-checks, and `ava start` is idempotent to retry.",
        file=sys.stderr,
    )


def _print_non_critical_unready_services(specs: tuple[ServiceSpec, ...]) -> None:
    """Name the non-critical services that missed their short window.

    The counterpart of `_print_unready_services` for the tier that cannot fail
    the start. The cross must still appear — the tier downgrade is a verdict
    change, never a silence — and it points at the alert that was posted.
    """
    names = ", ".join(session_name(s.session) for s in specs)
    print(
        f"\n✗ {len(specs)} non-critical service(s) not ready within "
        f"{NON_CRITICAL_SERVICE_READY_TIMEOUT_S:.0f}s: {names}\n"
        f"  They do not fail this start (the readiness gate waits for the critical roster "
        f"only),\n"
        f"  but an alert has been posted; the watchdog keeps trying to revive them and "
        f"`ava start` is idempotent to retry.",
        file=sys.stderr,
    )


_NON_CRITICAL_ALERTNAME = "non-critical service not ready after start"


def _alert_db_connect() -> Any:
    """The DB dial the non-critical alert uses — a named seam.

    `shared.db.connect` is a process-wide entry a full `ava start` dials in
    other steps too (the rollout-boundary lease read), so stubbing the alert's
    data plane must not replace every caller's. Indirection costs one line and
    keeps a test able to fake only this alert's DB.
    """
    import shared.db

    return shared.db.connect()


def _unresolved_alert_instance(conn: Any, service: str) -> tuple[str, str] | None:
    """(starts_at, fingerprint) of one open `start-readiness` instance for `service`,
    or None. Re-firing the same failure must UPDATE the open instance, not insert
    a fresh one — the boot job retries every 60 s with no cap (health-probe
    pattern, `services/heartbeat/liveness.py`)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at, fingerprint FROM alerts "
            "WHERE labels->>'alertname' = %s AND labels->>'service' = %s "
            "AND status = 'unresolved' ORDER BY starts_at DESC LIMIT 1",
            (_NON_CRITICAL_ALERTNAME, service),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _alert_upsert_and_maybe_im(conn: Any, alert: dict[str, object], *, im_enabled: bool) -> None:
    """One upsert + one IM (when the transition gate says so and IM is on)."""
    from shared.alerts import (
        display_language,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    text = notify_text(alert, display_language(conn))
    key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source="start-readiness")
    # `should_notify` already carries the retry gate (a firing instance stays
    # notifiable until notified_at is stamped — a failed IM is re-sent by the
    # next start), and a resolved edge for an instance that never fired stays
    # silent. So the IM push is purely `should_notify and im_enabled`.
    if should_notify and im_enabled and notify_im(text):
        stamp_notified(conn, [key])


def _notify_non_critical_unready_services(
    specs: tuple[ServiceSpec, ...], *, im_enabled: bool
) -> None:
    """Post one alerts row per non-critical service that missed its window.

    The tier's second rail: the demotion must not go silent. One instance PER
    SERVICE (so the resolved edge can match the recovered service), reused while
    the failure stays open. `im_enabled` gates only the IM push — the alerts row
    is always written: the boot job's uncapped 60 s retries run with
    `--no-readiness-gate` and must not spam the user's IM (QA #1196 P1-1). A
    DB/IM failure degrades to a printed note, never to silence elsewhere.
    """
    from datetime import UTC, datetime

    from shared.alerts import (
        fingerprint as compute_fingerprint,
    )

    now = datetime.now(UTC)
    for spec in specs:
        service = session_name(spec.session)
        alert: dict[str, object] = {
            "status": "firing",
            "labels": {
                "alertname": _NON_CRITICAL_ALERTNAME,
                "severity": "warning",
                "service": service,
            },
            "annotations": {
                "summary": (
                    f"non-critical service not ready within "
                    f"{NON_CRITICAL_SERVICE_READY_TIMEOUT_S:.0f}s of ava start: {service}"
                )
            },
            "starts_at": now.isoformat(),
            "fingerprint": compute_fingerprint(
                {"alertname": _NON_CRITICAL_ALERTNAME, "service": service}
            ),
        }
        try:
            with _alert_db_connect() as conn:
                open_instance = _unresolved_alert_instance(conn, service)
                if open_instance is not None:
                    # Same failure still open: reuse its identity so the upsert
                    # updates the row instead of inserting a duplicate.
                    alert["starts_at"] = open_instance[0]
                    alert["fingerprint"] = open_instance[1]
                _alert_upsert_and_maybe_im(conn, alert, im_enabled=im_enabled)
                conn.commit()
        except Exception as exc:
            print(
                f"  ! non-critical service alert failed ({type(exc).__name__}): "
                f"see the start log — {service} is still down",
                file=sys.stderr,
            )


def _recovered_non_critical_specs(
    started: tuple[ServiceSpec, ...], failed: tuple[ServiceSpec, ...]
) -> tuple[ServiceSpec, ...]:
    """The launched non-critical services that are serving now (the resolved
    edge's roster): started minus the critical manifest minus the failures the
    wait just returned."""
    failed_set = set(failed)
    return tuple(
        s for s in started if s.session not in CRITICAL_SERVICE_SESSIONS and s not in failed_set
    )


def _resolve_recovered_non_critical_alerts(
    specs: tuple[ServiceSpec, ...], *, im_enabled: bool
) -> None:
    """Close the open `start-readiness` instances of services that are up again.

    The resolved edge (QA #1196 P1-1): an instance left open would stay on the
    Inspector's unresolved panel after the service recovered (user ruling
    2026-08-29). Every start observes the roster, so the start that finds the
    service up resolves it — same resolve-on-recovery pattern as the health
    probes; a watchdog-only revival stays open until the next start.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for spec in specs:
        service = session_name(spec.session)
        try:
            with _alert_db_connect() as conn:
                open_instance = _unresolved_alert_instance(conn, service)
                if open_instance is None:
                    continue
                alert: dict[str, object] = {
                    "status": "resolved",
                    "labels": {
                        "alertname": _NON_CRITICAL_ALERTNAME,
                        "severity": "warning",
                        "service": service,
                    },
                    "annotations": {"summary": f"non-critical service is up again: {service}"},
                    "starts_at": open_instance[0],
                    "ends_at": now.isoformat(),
                    "fingerprint": open_instance[1],
                }
                _alert_upsert_and_maybe_im(conn, alert, im_enabled=im_enabled)
                conn.commit()
        except Exception as exc:
            print(
                f"  ! non-critical service alert resolve failed ({type(exc).__name__}): "
                f"see the start log — {service}",
                file=sys.stderr,
            )


def _print_service_row(spec: ServiceSpec, name_w: int, skip_reason: str | None = None) -> None:
    # Lazy import keeps tests' `monkeypatch.setattr(cli.commands, "_has_session", X)`
    # effective — the call goes through the package namespace which the test
    # rebinds, instead of the sub-module's frozen local binding.
    import cli.commands as _ns

    sess = session_name(spec.session)
    session_mark = "✓" if _ns._has_session(sess) else "✗"

    probe = _probe_service(spec)
    if probe.alive is True:
        probe_mark = "✓"
    elif probe.alive is False:
        probe_mark = "✗"
    else:
        probe_mark = "·"

    # A gated-out service (e.g. browser) is shown WITH its reason rather than
    # hidden. The marks still reflect real liveness, so a still-running but now-
    # gated session reads as "✓ ... -- skipped: <reason>", surfacing the mismatch.
    # A failing probe's detail rides on the same line: "down" and "answering, but
    # its home is /home/ava/.ava" call for completely different actions, and the
    # operator has no other place to learn which one they are looking at.
    suffix = f"   -- skipped: {skip_reason}" if skip_reason else ""
    if not suffix and probe.detail:
        suffix = f"   -- {probe.detail}"
    print(f"{sess.ljust(name_w)}  {session_mark}     {probe_mark} ({probe.label}){suffix}")


def _cluster_pin_status() -> tuple[str, str | None] | None:
    """For the `ava status` cluster-pin line: returns `(target_sha, this_host_head)`
    — `this_host_head` is None when the prod source HEAD can't be read. Returns None
    when no rollout has pinned a commit yet, or the pin can't be read at all.

    `ava status` is a host diagnostic command that must survive any failure of the
    pin subsystem (DB down, missing row, schema error), so the broad catch here is
    a deliberate CLI-boundary guard: the pin line is best-effort and is simply
    omitted on any error rather than aborting the whole status screen."""
    from shared.cluster_pin import get_cluster_target_sha

    try:
        pin = get_cluster_target_sha()
    except Exception:
        return None
    if pin is None:
        return None
    return pin, _prod_source_head_sha()
