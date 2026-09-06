"""Daemon HTTP endpoint — shared `/healthz` + optional daemon-specific routes.

Concurrent-loop trackers live in ``shared.loop_health`` after this module's split.

Long-running daemons (scheduler / agent-host / labeler /
memory_indexer) previously probed via pidfile. Problem: daemon
startup order is schema check -> init_gateway_process -> pidfile
write; in the 1-2s init phase the watchdog has already polled once,
does not see the pidfile -> misjudges "daemon dead" ->
subprocess.Popen starts another daemon -> multiple processes run
simultaneously, race-write the pidfile -> daemon spawns repeatedly in
a death loop.

Fix matches gateway (#251): probe via HTTP `/healthz` — when the
daemon binds the port it is a socket-layer signal, truly meaning
"ready to serve requests"; more stable than pidfile.

Design:
- ``start_health_server(name, port, extra_routes=...)`` starts the
  server in the caller's asyncio loop and returns an
  ``asyncio.Server``; the caller's finally block calls
  ``stop_health_server`` for cleanup
- bind ``127.0.0.1``-only; daemon healthchecks / RPC are strictly local
- ``/healthz`` is always present: it wraps the identity fields
  ``{"name": ..., "pid": ..., "home": ..., "started_at": ...}`` in the
  shared health envelope, including readiness, liveness, per-component progress,
  and reasons for degradation. The first three identity fields remain what
  ``probe_daemon`` verifies. ``Liveness`` tracks one loop; ``LivenessGroup``
  tracks concurrent loops independently and exposes their snapshots. Stale
  liveness or a degraded component flips the response to 503 so a live sibling
  cannot hide wedged work from the watchdog.
- ``extra_routes``: ``{("POST", "/search"): handler}`` lets a daemon
  expose its own RPCs (e.g. memory_indexer ``/search``, bypassing
  milvus-lite single-process flock restrictions)
- No new dependency — uses stdlib ``asyncio.start_server`` +
  hand-written HTTP parsing

The probe side lives here too (``probe_daemon``): a 200 is only believed
when the body's ``name``/``home``/``pid`` identify the answering process
as *this* unit's daemon. See that function's docstring for the outage
that motivated it.

``probe_home`` is the weaker sibling that verifies ``home`` alone — the
gateway's ``/api/health``, where uvicorn's reload fork makes a pid comparison
meaningless. It leaves ``name`` unchecked too, but only until the field has
rolled out fleet-wide; see that function's docstring for why the order matters.
Both share ``_health_payload``, so "what a 200 has to contain before it is
believed" is decided once.

A failed identity check splits into two verdicts (``ProbeVerdict``): a stray
of our own unit is ``DOWN`` and a respawn fixes it, while a port held by
another unit's daemon is ``PORT_TAKEN`` — terminal, because no respawn this
unit can perform will ever free that port.

``home`` is the identity of a **unit**, not of a cluster, and the difference is
load-bearing: on 2026-07-26 two agent-runners OF THE SAME CLUSTER shared one
machine's localhost namespace (a WSL2 distro whose loopback Windows can reach),
so a mismatch here named a same-cluster neighbour. The message says "another
unit's daemon" for that reason — reading it as "another cluster" sent the first
diagnosis after port ALLOCATION, which was never the mechanism (issue #977).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from shared import process_sha
from shared.config import get_field
from shared.daemon_http import RouteHandler as RouteHandler
from shared.daemon_http import start_daemon_http
from shared.env_registry import health_port_env_aliases
from shared.loop_health import LivenessGroup, LoopProgress  # noqa: F401  # pyright: ignore
from shared.paths import ava_home, legacy_pid_path
from shared.port_block import LEGACY_AVA_PORTS

# Re-export trackers moved to loop_health after this module crossed the 800-line ceiling.

_log = logging.getLogger("shared.daemon_health")

# ── Health-port single source (S4 isolation, F-s4-3) ──────────────────────────
# Four tables used to describe the same fact (per-unit daemon health ports) and
# drifted apart — `im_bridge` / `delivery_watchdog` were added to settings and
# DEFAULT_PORTS but never to the env-var derive surface, so `--health-port-base`
# did not move them (F-s4-2). The tables are now derived, never hand-maintained:
#
#   PORT_OFFSETS (shared.port_block)          — service -> block offset (ONE table)
#   health_port_env_aliases() (env_registry) — service -> env var, the derive surface
#   DEFAULT_PORTS (below)                     — the legacy fallback = the LEGACY_AVA_PORTS
#                                               subset for health daemons
#   _HEALTH_PORT_OVERRIDES (below)            — service -> settings field
#
# `tests/shared/test_cluster_env.py` guards that they stay in sync.

# The legacy shared-default segment (prod ~/.ava's fixed ports, 8102-8111) —
# the LEGACY_AVA_PORTS subset for daemons serving /healthz. The curl_url of
# ServiceSpec in cli/commands.py references the same ports. 8000 is reserved
# for gateway, 3000 for frontend. A unit that never declared a per-unit block
# binds these; `health_port()` warns on the fallback (F-s4-12).
DEFAULT_PORTS: dict[str, int] = {svc: LEGACY_AVA_PORTS[svc] for svc in health_port_env_aliases()}

# Settings field mapping — name -> settings attribute. Every health daemon's
# settings field follows `f"{name}_health_port"`, so the mapping is derived
# from _HEALTH_PORT_ENV rather than maintained twice.
_HEALTH_PORT_OVERRIDES: dict[str, str] = {
    svc: f"{svc}_health_port" for svc in health_port_env_aliases()
}

# Cap single request body size — 64 KB is enough for search query /
# RPC calls and blocks malicious large POSTs.
_MAX_BODY_BYTES = 64 * 1024


# Daemons already warned about the shared-default fallback in THIS process.
# A unit that never declared a per-unit block is a config gap worth one loud
# line per daemon, not one per health_port() call (healthchecks call it every
# round).
_warned_shared_default: set[str] = set()

# Daemons already warned about the Windows iphlpsvc 8106 collision (above).
_warned_windows_8106: set[str] = set()


def health_port(name: str) -> int:
    """Return the healthz port for daemon `name`.

    First reads ``settings.<name>_health_port`` (overridable by env
    ``AVA_<NAME>_HEALTH_PORT``); falls back to ``DEFAULT_PORTS``; if
    the name is not registered, raises ``KeyError`` (fail fast; new
    daemons must register a port first; silent fallback is not
    allowed).

    The fallback is deliberate but loud (F-s4-12): the 8100s are a SHARED
    segment, so a unit that never declared a per-unit block (no enroll
    ``--health-port-base``, no ``AVA_<NAME>_HEALTH_PORT`` in its own .env)
    quietly joins whatever co-located units also fell back — the exact
    2026-07-24/26 incident shape. The first fallback per daemon per process
    logs a warning naming the fix.
    """
    override_attr = _HEALTH_PORT_OVERRIDES.get(name)
    if override_attr is not None:
        override = get_field(override_attr)
        if override is not None:
            port = int(override)
        else:
            port = DEFAULT_PORTS[name]
            if name not in _warned_shared_default:
                _warned_shared_default.add(name)
                _log.warning(
                    "health port for %s is not declared in this unit's .env — using the "
                    "shared default %d (the 8100s are a shared segment; a co-located unit "
                    "on this localhost namespace may already hold it). Pin a per-unit port "
                    "with `ava enroll --health-port-base <block-base>` or set "
                    "AVA_%s_HEALTH_PORT in this unit's .env",
                    name,
                    port,
                    name.upper(),
                )
    else:
        port = DEFAULT_PORTS[name]
    if os.name == "nt" and port == 8106 and name not in _warned_windows_8106:
        # The Windows iphlpsvc service (svchost -k NetSvcs -s iphlpsvc) binds
        # 8106 at boot on Windows hosts; a daemon pointed there fails to bind
        # on every launch (2026-08-11, #1179 — ops default WAS 8106, moved to
        # 8113; win's events_maintenance was also hand-set to 8106). Checked
        # for BOTH the override and the default, so an explicit (mis)configured
        # AVA_<NAME>_HEALTH_PORT=8106 is named too — once per daemon.
        _warned_windows_8106.add(name)
        _log.warning(
            "health port 8106 for %s is held by the Windows iphlpsvc service - "
            "set AVA_%s_HEALTH_PORT to a free port or the daemon cannot bind",
            name,
            name.upper(),
        )
    return port


class Liveness:
    """Main-loop heartbeat for a daemon.

    The daemon's loop calls ``beat()`` each iteration; the ``/healthz``
    handler (same asyncio loop) reports 503 once the last beat is older
    than ``timeout_s``. A daemon whose process is alive but whose loop
    has stalled on a never-returning ``await`` therefore shows up as
    "dead" to the watchdog, which respawns it — closing the gap where a
    still-scheduling event loop kept answering 200 while doing no work.

    Uses ``time.monotonic()`` so wall-clock jumps / NTP steps / laptop
    sleep do not produce false staleness.

    Set ``timeout_s`` comfortably above the loop's worst-case
    *legitimate* iteration; the watchdog's own poll cadence bounds
    detection latency regardless, so the exact value is not delicate.
    """

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    def stale_for(self) -> float:
        return time.monotonic() - self._last

    def is_alive(self) -> bool:
        return self.stale_for() <= self._timeout_s


def _healthz_payload(
    name: str,
    pid: int,
    home: str,
    started_at: float,
    liveness: Liveness | LivenessGroup | None,
    components: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    """Build the shared GET /healthz envelope, preserving daemon identity.

    ``name``/``pid``/``home`` are the identity triple ``probe_daemon`` checks —
    a probe that only trusted the status code cannot tell this daemon apart from
    any other process that happens to hold the port. ``home`` is the daemon's
    ``$AVA_HOME``, i.e. its unit identity.

    ``sha`` is the commit this daemon froze at its own boot. It is answered by
    the daemon process itself, so unlike any on-disk bookmark it cannot be
    refreshed without a restart — which makes a per-daemon `curl /healthz` the
    way to tell a daemon still holding pre-rollout code from one that restarted
    onto it."""
    payload: dict[str, object] = {
        "name": name,
        "pid": pid,
        "home": home,
        "started_at": started_at,
        "sha": process_sha.get(),
    }
    from shared import health_schema

    stale_for = None
    if liveness is not None:
        stale_for = round(liveness.stale_for(), 1)
        payload["stale_for"] = stale_for
        if isinstance(liveness, LivenessGroup):
            payload["loops"] = liveness.snapshot()
    if components is None:
        loop: dict[str, object] = {
            "name": "loop",
            "status": health_schema.OK if liveness is None or liveness.is_alive() else "stale",
        }
        if stale_for is not None:
            loop["stale_for"] = stale_for
        components = [loop]
    return health_schema.render(payload, components, liveness=liveness, extra=extra)


async def start_health_server(
    name: str,
    port: int | None = None,
    *,
    host: str = "127.0.0.1",
    extra_routes: Mapping[tuple[str, str], RouteHandler] | None = None,
    liveness: Liveness | LivenessGroup | None = None,
    components: list[dict[str, object]] | Callable[[], list[dict[str, object]]] | None = None,
    extra: dict[str, object] | Callable[[], dict[str, object]] | None = None,
    auth_token: str | None = None,
) -> asyncio.Server:
    """Start daemon HTTP server; return server instance (caller is responsible for close).

    Args:
        name: daemon name (used in ``/healthz`` response `name` field + log)
        port: explicit port; None -> infer via ``health_port(name)``
        host: bind interface. Default ``127.0.0.1`` (healthcheck / local RPC
            only). The agent-runner ops server passes ``0.0.0.0`` so the
            gateway can dial it over the private network; an open port on the
            trusted cluster's private network is the same posture as the
            gateway's own ``0.0.0.0`` bind.
        extra_routes: optional daemon-specific routes,
            ``{(method, path): handler}``. handler is an async
            callable taking body bytes and returning (status, body,
            content_type). On handler exception -> 500. Body size
            exceeding 64 KB -> 413 (actually returned as 400 for
            simplicity).
        liveness: optional main-loop heartbeat or concurrent-loop group; when
            it is not alive, ``/healthz`` returns 503 instead of 200 so the
            watchdog respawns a wedged daemon. Groups also expose per-loop
            progress snapshots in the response.
        components: optional per-component health state. A callable is evaluated
            for each request, so a daemon can report fresh worker progress without
            sharing mutable response state with this server.
        extra: optional non-component health fields. As with ``components``, a
            callable is evaluated on each request.
        auth_token: when set, every ``extra_routes`` request must carry
            ``Authorization: Bearer <auth_token>`` or it gets 401. ``/healthz``
            stays unauthenticated (the watchdog probes it locally and it leaks
            no secret). The ops server passes the cluster secret here when
            multi-host is on, so a LAN peer cannot drive /ops without it.
    """
    if port is None:
        port = health_port(name)
    started_at = time.time()
    pid = os.getpid()
    # Resolved once at bind time, not per request: ava_home() mkdirs on every call.
    home = str(ava_home())

    def health_response() -> tuple[int, bytes]:
        current_components = components() if callable(components) else components
        current_extra = extra() if callable(extra) else extra
        return _healthz_payload(
            name, pid, home, started_at, liveness, current_components, current_extra
        )

    return await start_daemon_http(
        host=host,
        port=port,
        health_response=health_response,
        extra_routes=extra_routes,
        auth_token=auth_token,
    )


class ProbeVerdict(Enum):
    """What one probe concluded — and, when not alive, whether a respawn can help.

    ``DOWN`` and ``PORT_TAKEN`` are both "not alive", but they are categorically
    different conditions and only one of them is a healthcheck's business:

    - ``DOWN`` — nothing this unit can identify holds the port (refused, timed
      out, 503, or a stray answering under *this* unit's own home+name).
      ``respawn_service`` kills this unit's ``ava-<svc>`` session first, so a
      respawn is exactly the fix.
    - ``PORT_TAKEN`` — the port answers as something outside this unit's
      reach: another ``$AVA_HOME``'s daemon, a different daemon kind, or a
      process that is not an Ava ``/healthz`` at all. This unit cannot kill it
      (session records live under ``$AVA_HOME/run/sessions``, so a foreign
      unit's sessions are in a home this one never touches), so every respawn
      walks into ``[Errno 48]
      Address already in use`` and cannot ever verify. Retrying is not
      remediation, it is a loop.

    The line is the UNIT, not the cluster: two agent-runners of one cluster on
    one machine are as unkillable to each other as two clusters would be.

    The distinction lives here rather than in a ``startswith("identity
    mismatch")`` read of ``detail``, which would be a second definition of the
    same fact in every caller that wanted to branch on it."""

    ALIVE = "alive"
    DOWN = "down"
    PORT_TAKEN = "port-taken"


# Exit codes a daemon healthcheck's `main()` uses, so the watchdog's
# "healthcheck <x> reported failure (exit N)" line (and any OS scheduler running
# the module standalone) can tell "still trying" from "needs a human". 2 is left
# alone — argparse's usage-error code.
EXIT_RESPAWN_FAILED = 1
EXIT_PORT_TAKEN = 3


@dataclass(frozen=True)
class DaemonProbe:
    """Outcome of one ``/healthz`` identity probe.

    ``detail`` is populated on EVERY outcome so a caller can log *why* — the
    outage below ran for 98 minutes emitting nothing but "daemon alive".

    Build via ``up()`` / ``down()`` / ``port_taken()``; each reads at the call
    site as the verdict it is, where a bare positional would not. ``alive`` and
    ``terminal`` are derived from ``verdict``, so the two questions a caller asks
    ("is it serving?", "can I fix it?") cannot drift apart."""

    verdict: ProbeVerdict
    detail: str

    @property
    def alive(self) -> bool:
        return self.verdict is ProbeVerdict.ALIVE

    @property
    def terminal(self) -> bool:
        """No respawn this process can perform will change this verdict.

        True only for ``PORT_TAKEN``. A caller that sees this must report and
        stop, not retry — see ``ProbeVerdict`` for why respawning cannot win.
        Nothing is persisted to remember it: the state self-clears, because once
        the occupant goes away the next probe simply reads ``DOWN`` and the
        normal respawn path runs."""
        return self.verdict is ProbeVerdict.PORT_TAKEN

    @classmethod
    def up(cls, detail: str) -> DaemonProbe:
        return cls(verdict=ProbeVerdict.ALIVE, detail=detail)

    @classmethod
    def down(cls, detail: str) -> DaemonProbe:
        return cls(verdict=ProbeVerdict.DOWN, detail=detail)

    @classmethod
    def port_taken(cls, detail: str) -> DaemonProbe:
        """Something outside this unit's reach holds the port — terminal."""
        return cls(verdict=ProbeVerdict.PORT_TAKEN, detail=detail)


# The probe's own HTTP timeout. Healthz is a loopback JSON dump; anything slower
# than this is a wedged process, which is "dead" for watchdog purposes.
_PROBE_TIMEOUT_S = 5.0


def _recorded_pid(pidfile: Path, name: str) -> int | None:
    """This unit's recorded pid for daemon ``name``, or None when no readable
    pidfile exists.

    Checks the configured path first, then the legacy ``$AVA_HOME/<name>.pid``
    location — the same two-path lookup each daemon's own ``_is_running`` guard
    performs, so the probe and the singleton guard can never disagree about which
    file is authoritative."""
    for path in (pidfile, legacy_pid_path(name)):
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            continue
    return None


def _health_payload(url: str, timeout_s: float) -> dict[str, object] | DaemonProbe:
    """The JSON object a health endpoint answered with, or the not-alive verdict
    that trying to read one produced.

    The single place that decides how much of a 200 has to be true before any
    identity check even gets to run — shared by ``_probe_daemon`` and
    ``_probe_home`` so the two cannot drift on "is this an Ava health endpoint at
    all". A foreign process on the port can return anything, so the body is parsed
    defensively and the mismatch reported rather than raised out of a cron-invoked
    healthcheck.

    Which verdict each failure earns follows ``ProbeVerdict``: an unreachable port
    or a non-2xx is ``DOWN`` (our own daemon, absent or wedged — a respawn is the
    fix), while a 200 whose body is not an Ava payload at all is ``PORT_TAKEN``
    (something we cannot kill answers there, so respawning only walks into the
    bound port).
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 — loopback URL from settings
            if not (200 <= resp.status < 300):
                return DaemonProbe.down(f"healthz returned HTTP {resp.status}")
            body = resp.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        detail = f"healthz returned HTTP {exc.code}"
        try:
            parsed = json.loads(exc.read(_MAX_BODY_BYTES))
        except json.JSONDecodeError:
            return DaemonProbe.down(detail)
        if isinstance(parsed, dict):
            reasons_raw = cast("dict[str, object]", parsed).get("degraded_reasons")
            if isinstance(reasons_raw, list):
                reasons = "; ".join(
                    reason
                    for reason in cast("list[object]", reasons_raw)
                    if isinstance(reason, str)
                )
                if reasons:
                    detail += f"; degraded: {reasons}"
        return DaemonProbe.down(detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return DaemonProbe.down(f"healthz unreachable: {type(exc).__name__}: {exc}")
    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError:
        return DaemonProbe.port_taken(f"healthz body is not JSON — foreign process on {url}")
    if not isinstance(parsed, dict):
        return DaemonProbe.port_taken(
            f"healthz body is not a JSON object — foreign process on {url}"
        )
    return cast("dict[str, object]", parsed)


def probe_home(url: str, *, timeout_s: float = _PROBE_TIMEOUT_S) -> DaemonProbe:
    """Probe a health endpoint that carries ``home`` but no ``name``/``pid``,
    ALWAYS returning a verdict.

    The total wrapper (same contract as ``probe_daemon``); ``_probe_home`` is the
    probe. Used for the gateway's ``/api/health``, and it is the probe where
    losing a verdict costs most: the gateway is what the cluster health probe
    polls, with ``--auto-rollback --threshold 3`` armed against it.
    """
    try:
        return _probe_home(url, timeout_s=timeout_s)
    except Exception as exc:
        _log.exception("[probe_home] %s probe raised unexpectedly; treating as down", url)
        return DaemonProbe.down(f"probe raised {type(exc).__name__}: {exc}")


def _probe_home(url: str, *, timeout_s: float = _PROBE_TIMEOUT_S) -> DaemonProbe:
    """2xx from ``url`` AND a ``home`` matching this unit's ``$AVA_HOME``.

    The same failure mode ``probe_daemon`` exists for — a 200 only proves
    *something* holds the port — but with the ``name``/``pid`` arms not checked.
    The two are not the same kind of omission:

    - ``pid`` is dropped **by decision**. The gateway runs uvicorn with reload
      configurable, so it answers from a worker forked out of the process that
      wrote ``gateway_pidfile``: a legitimate gateway routinely reports a pid its
      own pidfile never recorded, making the comparison meaningless.
    - ``name`` is **pending, not rejected**. ``/api/health`` now emits
      ``name="gateway"``, but adding the arm here is the contract half of an
      expand-contract pair (#1038) and may only land once every deployed gateway
      emits the field. A rolling upgrade updates agent-runners and the gateway at
      different moments; a ``name`` mismatch is ``PORT_TAKEN``, which is terminal
      (``decisions/2026-07-29-identity-mismatch-is-terminal.md``), so a runner
      on new code probing a gateway still on old code would read ``name=None`` and
      terminal-fail a perfectly healthy gateway — the very thing ``ava cluster
      health-probe`` polls with ``--auto-rollback --threshold 3`` armed. **Do not
      add the arm without confirming the payload has rolled out fleet-wide.**

    So today ``home`` carries the check alone: it is stable across the reload fork
    and is the property an impostor from another unit actually fails.

    A ``home`` mismatch is ``PORT_TAKEN`` for the same reason it is in
    ``_probe_daemon``: the occupant lives in another unit's session, so no
    respawn this unit performs can free the port.
    """
    payload = _health_payload(url, timeout_s)
    if isinstance(payload, DaemonProbe):
        return payload
    got_home, expected_home = payload.get("home"), str(ava_home())
    if got_home != expected_home:
        return DaemonProbe.port_taken(
            f"identity mismatch on {url}: home={got_home!r} != {expected_home!r} — "
            "another unit's daemon holds this port"
        )
    return DaemonProbe.up(f"home {expected_home}")


def probe_daemon(
    name: str, url: str, *, pidfile: Path, timeout_s: float = _PROBE_TIMEOUT_S
) -> DaemonProbe:
    """Probe a daemon's ``/healthz`` and verify the answering process is ours,
    ALWAYS returning a verdict.

    This is the total wrapper; ``_probe_daemon`` below is the probe itself. The
    watchdog's contract needs a ``DaemonProbe``, not an exception: it isolates
    each check, so a raising probe does not take the round down — it just means
    the service is never judged alive-or-dead, **so no restart is ever
    attempted**, while every 60s round writes a fresh multi-KB traceback. That is
    the failure `browser-mcp` ran in on the `win` runner until 2026-07-29, whose
    watchdog log reached 50 MB.

    Six healthchecks route through here (heartbeat, labeler, memory-indexer, ops,
    events-maintenance, agent-host), so a single escaping exception type silences
    six services' revival at once. The inner probe's narrow catches are kept —
    they produce the good operator-facing ``detail`` strings — and anything they
    miss is caught here, logged with a traceback, and reported as down.
    """
    try:
        return _probe_daemon(name, url, pidfile=pidfile, timeout_s=timeout_s)
    except Exception as exc:
        # Down, not "unknown": a verdict the watchdog can act on beats silence.
        # An unforeseen probe failure is not evidence of a healthy daemon.
        _log.exception("[probe_daemon] %s probe raised unexpectedly; treating as down", name)
        return DaemonProbe.down(f"probe raised {type(exc).__name__}: {exc}")


def _probe_daemon(
    name: str, url: str, *, pidfile: Path, timeout_s: float = _PROBE_TIMEOUT_S
) -> DaemonProbe:
    """Probe a daemon's ``/healthz`` AND verify the answering process is ours.

    HTTP 200 only proves *something* listens on that port. On 2026-07-24 a
    pytest-leaked restarter daemon — a different ``$AVA_HOME``, but fallen back
    to prod's default 8102 because the test home allocated no health-port block —
    answered 200 for 98 minutes while prod's own restarter was dead. The watchdog
    saw green every round, never respawned, and every `restarting` agent in the
    cluster stayed frozen. A status-code-only probe cannot distinguish that from
    health, so the body's identity is checked too:

    - ``name`` — the daemon kind asked for.
    - ``home`` — the ``$AVA_HOME`` the answering process runs under, i.e. its
      unit identity. This is what an impostor from another unit fails, and it is
      the only signal that works for a daemon whose pid legitimately differs from
      its pidfile (a supervisor that forks its server).
    - ``pid`` — must equal this unit's pidfile, catching a same-home stray too.

    Anything that is not a positive match — non-2xx, a non-JSON body, missing
    keys, a missing pidfile — is not alive. A missing pidfile is deliberately NOT
    treated as "unverifiable, assume alive": every daemon writes its pidfile
    *before* binding ``/healthz``, so 200-without-a-pidfile is never our own
    daemon mid-startup — something else is answering.

    **Which not-alive verdict is which** (see ``ProbeVerdict``): the line is
    whether killing this unit's own ``ava-<svc>`` session — what a respawn
    does first — can free the port.

    - ``name`` or ``home`` mismatch, or a body that is not an Ava ``/healthz`` at
      all → ``PORT_TAKEN``. The occupant is not in the session we would kill
      (session records live under ``$AVA_HOME/run/sessions``, so a foreign
      unit's sessions are in a home this one never touches), so every
      respawn crashes on the bound port and no probe can ever verify it.
    - a ``pid`` that this unit's pidfile does not record, and a missing pidfile,
      stay ``DOWN``. Both are reached only *after* name and home matched, so the
      answering process belongs to this unit and this daemon kind — a stray of
      our own, which the respawn's kill-session does clear. Retrying is real
      remediation there.

    Note ``urlopen`` raises ``HTTPError`` for 4xx/5xx, so a liveness-stale 503
    lands in the unreachable branch — ``DOWN``, which is right: that is our own
    daemon with a wedged loop, and respawning it is the intended cure.

    ``_health_payload``'s narrow catches are for the failures with a *useful*
    detail string. They are not the safety net: ``urlopen``/``read`` also raise
    ``http.client.HTTPException`` (a malformed status line or a truncated body —
    neither an ``OSError``), and ``_recorded_pid`` can hit an ``OSError`` this
    frame does not wrap. ``probe_daemon`` is the wrapper that guarantees a
    verdict regardless.
    """
    payload = _health_payload(url, timeout_s)
    if isinstance(payload, DaemonProbe):
        return payload

    got_name, got_home, got_pid = payload.get("name"), payload.get("home"), payload.get("pid")
    if got_name != name:
        return DaemonProbe.port_taken(
            f"identity mismatch on {url}: name={got_name!r} != {name!r} — "
            "a different daemon kind holds this port"
        )
    expected_home = str(ava_home())
    if got_home != expected_home:
        return DaemonProbe.port_taken(
            f"identity mismatch on {url}: home={got_home!r} != {expected_home!r} — "
            "another unit's daemon holds this port"
        )
    recorded = _recorded_pid(pidfile, name)
    if recorded is None:
        return DaemonProbe.down(
            f"{url} answers but no pidfile at {pidfile} — not this unit's daemon"
        )
    if got_pid != recorded:
        return DaemonProbe.down(
            f"identity mismatch on {url}: healthz pid={got_pid!r} != pidfile pid={recorded} — "
            "a stray process holds this port"
        )
    # The commit rides along in the detail, never in the verdict: a daemon on
    # stale code is *alive*, and failing it here would have every watchdog
    # respawn its daemon the moment a rollout advances the checkout, racing the
    # orchestrated restart. Surfacing it is the job; acting on it is not.
    got_sha = payload.get("sha")
    if isinstance(got_sha, str) and got_sha:
        return DaemonProbe.up(f"pid {recorded}, code {got_sha[:7]}")
    return DaemonProbe.up(f"pid {recorded}")


async def stop_health_server(server: asyncio.Server) -> None:
    """Close health server (idempotent + does not raise). Called in daemon finally."""
    server.close()
    # In cleanup, swallow any cleanup error — close itself is
    # idempotent; second wait_closed call may raise RuntimeError
    # (event loop closed) etc.
    with contextlib.suppress(Exception):
        await server.wait_closed()
