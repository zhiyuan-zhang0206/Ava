"""machines table read/write — multi-machine deployment registry of
(name, role, inbound base URL).

The `machines` row (keyed by name) is a COMPOSED read model: it is recomputed
from the host's per-unit contributions in `machine_units` (keyed by
(machine_name, home), home = the unit's $AVA_HOME). Each unit UPSERTs only its
own `machine_units` row via `register_self()` — at the tail of `ava start`, and
again when the unit's ops daemon reaches serving state at its own boot — then
`_recompute_machine_row()` rewrites the `machines` row as the union over the
machine's non-stopped units. This lets TWO co-located units on one host (a
gateway-only unit under `~/.ava_gateway` + an agent-runner-only unit under
`~/.ava`) compose into ONE machine row whose capability set is their union,
instead of clobbering each other on every `ava start`. Every `machines` reader
is unchanged — it reads the composed row.

`gateway_url` is the machine's inbound base URL that the rest of the cluster
dials:
- gateway: its public/private-network gateway URL.
- agent-runner: its ops server URL `http://<reachable-host>:<ops_port>`, which the
  gateway POSTs cluster ops to (`gateway/cluster_rpc.py`).
On an agent-runner-capable host the composed `gateway_url` holds the ops URL (the
dial target spawn/lifecycle forwarding POSTs to), matching the single-box
behavior before the split.

Local URL source for gateway = env `AVA_GATEWAY_URL` > file
`$AVA_HOME/gateway_url` > raise GatewayUrlMissing (same precedence as
[[machine]] / [[memory_repo]]).
"""

from __future__ import annotations

from urllib.parse import urlparse

import psycopg

import shared.db
from shared.agents import MachineNotRegistered
from shared.machine import (
    MachineRoles,
    _resolve_gateway_url,
    is_agent_runner,
    is_gateway,
    is_observability_station,
    machine_description,
    machine_name,
)
from shared.netutil import is_loopback_host
from shared.paths import ava_home

__all__ = [
    "GatewayUrlMissing",
    "LoopbackDialUrlRefused",
    "MachineGatewayUrlMissing",
    "MachineNotRegistered",
    "clear_stopped_marker",
    "gateway_url",
    "is_paused",
    "list_agent_runners",
    "list_all",
    "list_paused",
    "list_stopped_agent_runners",
    "lookup",
    "lookup_role",
    "pause",
    "register_self",
    "resume",
    "unit_dial_url",
]


class GatewayUrlMissing(RuntimeError):  # noqa: N818 — state description; same style as [[MachineNameMissing]] / [[MemoryRemoteMissing]]
    """Neither env `AVA_GATEWAY_URL` nor `$AVA_HOME/gateway_url` is set
    — multi-machine setup is incomplete.

    Local setup error, not on the wire (same style as
    [[MachineNameMissing]] / [[MemoryRemoteMissing]]).
    """


class MachineGatewayUrlMissing(RuntimeError):  # noqa: N818 — state description; same style as MachineNotRegistered
    """The machine row exists but does not advertise a gateway URL."""


class LoopbackDialUrlRefused(RuntimeError):  # noqa: N818 — state description; same style as GatewayUrlMissing
    """An agent-runner-only or observability-station-only unit tried to register a
    loopback dial URL (localhost / 127.* / ::1) in the central `machines` table.

    A loopback dial URL is only reachable from the same box, so it is legal solely
    for a single co-located unit (a `gateway,agent-runner` or
    `gateway,observability-station` host, or a zero-config single box) whose own
    gateway self-dials it over loopback. For a remote (split-deployment) runner or
    station it is a misconfiguration — the gateway would dial *itself* instead of
    the peer and, if a co-located service answers, silently report the peer online
    under the wrong identity (the 2026-07-18 runner incident; the station variant
    is WP4). This is the central-DB integrity guard that stops such a row from ever
    being written, whichever host runs `register_self`; the operator sets
    `AVA_MACHINE_HOST` (or `ava enroll --machine-host`) to this host's reachable
    address.
    """


def gateway_url() -> str:
    """Get this host's gateway URL. env > file > raise.

    Precedence:
    1. `AVA_GATEWAY_URL` env var (headless deployment / CI / docker)
    2. `$AVA_HOME/gateway_url` file (regular setup; `ava start`
       first run writes it)
    3. neither -> GatewayUrlMissing (`ava start` precheck catches and
       TTY-prompts the default `http://<reachable-host>:8000`)

    Raises:
        GatewayUrlMissing: env var empty + file missing or empty.

    Delegates resolution to shared.machine._resolve_gateway_url() so this answer
    never drifts from gateway_api_base(); only the raised exception type differs.
    """
    url = _resolve_gateway_url()
    if url is None:
        raise GatewayUrlMissing(
            f"gateway URL not set — `ava start` will prompt during setup. "
            f"Manual: `echo <url> > {ava_home() / 'gateway_url'}` or `export AVA_GATEWAY_URL=<url>`."
        )
    return url


def _reject_loopback_dial_url(
    url: str | None,
    *,
    serve_gateway: bool,
    serve_agent_runner: bool,
    serve_observability_station: bool,
) -> None:
    """Refuse to register a loopback dial URL for a unit the gateway must reach
    over the network.

    A loopback dial URL (localhost / 127.* / ::1) is only reachable from the same
    box, so it is legal exactly when the gateway is co-located with this unit and
    illegal when the gateway is remote — the split-deployment misconfiguration where
    the gateway would dial itself and, if a co-located service answers, report the
    peer online under the wrong identity (the 2026-07-18 runner incident; the
    station variant is the same shape — the gateway would probe itself instead of
    the station). The location of the gateway is read from THIS unit's configured
    gateway URL (see conventions/reachability-and-credentials.md, rule 2):

    - `url` None, or this unit also serves gateway → never a misconfig (skip).
    - this unit is agent-runner-only or observability-station-only, `url` loopback,
      and the gateway URL is itself loopback → gateway is co-located (a split-home
      single box) → legal (skip).
    - this unit is agent-runner-only or observability-station-only, `url` loopback,
      and the gateway URL is a remote address → the unit is on another box →
      REJECT before the DB write.
    - gateway URL unresolvable → cannot prove remoteness, so do not reject here (the
      enroll-time check in `cli/enroll.py` is the primary guard, with both the
      gateway URL and the machine-host in hand).

    Raises:
        LoopbackDialUrlRefused: agent-runner-only or observability-station-only unit
            with a loopback `url` while its gateway is provably remote.
    """
    if url is None or serve_gateway or not (serve_agent_runner or serve_observability_station):
        return
    if not is_loopback_host(urlparse(url).hostname or ""):
        return
    gateway = _resolve_gateway_url()
    if gateway is None or is_loopback_host(urlparse(gateway).hostname or ""):
        return
    raise LoopbackDialUrlRefused(
        f"{machine_name()!r} would register a loopback dial URL ({url}) while its "
        f"gateway is remote ({gateway}); the gateway cannot reach it there — it would dial itself. "
        "Set AVA_MACHINE_HOST (or re-run `ava enroll --machine-host <this host's reachable "
        "address>`) to this host's private-network address."
    )


def unit_dial_url(roles: MachineRoles) -> str | None:
    """The inbound base URL a unit carrying `roles` advertises — the address the
    rest of the cluster dials (conventions/reachability-and-credentials.md,
    endpoint advertisement).

    The single definition, shared by both callers of `register_self`: `ava start`
    (`cli/commands/_repo.py:_register_machine_or_die`, passing its resolved
    capability set) and the ops daemon's own boot registration
    (`services/agent_ops/daemon.py`, passing `machine_role()`). Two writers of one
    row that each computed this shape themselves would be free to advertise
    different addresses for the same unit, and the loser would be a host the
    gateway dials at an address nothing answers on.

    Every advertised URL is built on `reachable_host()` (env AVA_MACHINE_HOST >
    machine_host file > localhost), never on the bare gateway URL and never on a
    hardcoded loopback: a machine with a reachable identity must advertise it, or
    the page proxy's SSRF guard (which only dials a machine's advertised
    addresses) refuses every page server on the host (2026-08-30 serve 400).

    - agent-runner + gateway (single box): `http://<reachable_host()>:<ops_port>`.
      On a true single box `reachable_host()` resolves to localhost (env >
      machine_host file > localhost), so the zero-config shape is unchanged; on a
      multi-machine cluster it is the machine's private-network address — the
      address the rest of the cluster (and the page proxy's SSRF guard, which
      only dials registered machine addresses) reaches it at. The old
      unconditional-localhost special case advertised an un-dialable address on
      a gateway box with a reachable identity (2026-08-12 serve outage).
    - agent-runner only (split): `http://<reachable_host()>:<ops_port>`, so a
      remote gateway can reach it. `reachable_host()` resolves env > machine_host
      file > localhost, and a localhost fall-through here is a misconfiguration —
      `_reject_loopback_dial_url` refuses it at write time rather than persisting
      a dead row.
    - gateway only (with or without a station capability): the reachable-host
      form of its own gateway URL, `http://<reachable_host()>:<gateway port>`
      (informational — a pure gateway runs no ops server, so nothing dials it for
      ops; the hostname is what the page proxy's SSRF allowlist consumes). The
      port is the gateway URL's port when one is configured, else the gateway
      bind-port setting.
    - observability-station only: `http://<reachable_host()>:<OTLP ingress port>`
      — the station's bearer-authenticated OTLP ingress, the one station address
      remote consumers (the gateway collector relay and the station health probe)
      dial. The port follows `AVA_TELEMETRY_OTLP_PORT` (single source), so the
      advertised address and the ingress listener can never drift apart.

    The URL never raises for a missing gateway URL — the port falls back to the
    gateway bind-port setting, and the hostname is `reachable_host()` regardless
    (the advertisement must not depend on AVA_GATEWAY_URL being configured).
    """
    # Imported in-function, matching the call site this logic moved from: it keeps
    # `shared.machine.reachable_host` patchable by name (a module-level binding
    # here would freeze it at import) and keeps the health-server module off
    # `shared.machines`'s import path.
    from shared.daemon_health import health_port
    from shared.machine import reachable_host

    if "gateway" not in roles and "agent-runner" not in roles:
        return _station_ingress_url()  # pure observability-station: its OTLP ingress
    if "agent-runner" not in roles:
        return _gateway_reachable_url()
    host = reachable_host()
    return f"http://{host}:{health_port('ops')}"


def _gateway_reachable_url() -> str:
    """The reachable-host form of a gateway unit's advertised URL.

    `http://<reachable_host()>:<gateway port>` — the port is the configured
    gateway URL's port when one is resolvable, else the gateway bind-port
    setting. Deliberately NOT the bare `gateway_url()`: that URL may name
    loopback while this host carries a reachable identity (machine_host set),
    and the advertised hostname is what the page proxy's SSRF allowlist
    consumes — a loopback advertisement on such a host makes every page
    serve 400 (2026-08-30). The hostname comes from `reachable_host()`, so
    the zero-config single-box shape (localhost) is unchanged.
    """
    from shared.config import settings
    from shared.machine import reachable_host

    url = _resolve_gateway_url()
    port = urlparse(url).port if url else None
    if port is None:
        port = settings.gateway.gateway_port
    return f"http://{reachable_host()}:{port}"


def _station_ingress_url() -> str:
    """The advertised inbound URL of a pure observability-station unit.

    `http://<reachable_host()>:<OTLP ingress port>` — the station's
    bearer-authenticated OTLP ingress (the collector's `otlp/remote`
    receiver, `cli/commands/_otel_collector.py`), the one station address
    remote consumers dial. The port follows `AVA_TELEMETRY_OTLP_PORT`
    (single source, task #1945), so the advertisement and the listener can
    never drift apart. Loopback falls through when the station has no
    reachable identity — legal only when the gateway is co-located
    (`_reject_loopback_dial_url` enforces that at write time).
    """
    from shared.config import settings
    from shared.machine import reachable_host

    return f"http://{reachable_host()}:{settings.observability.telemetry_otlp_port}"


def register_self(url: str | None = None) -> None:
    """UPSERT THIS UNIT's machine_units row, then recompute the machines row.

    A unit is identified by (machine_name, home) where home = this unit's
    $AVA_HOME (`shared.paths.ava_home()`). The unit contributes its own caps
    (serve_gateway / serve_agent_runner / serve_observability_station), its dial
    `url` (resolved by `unit_dial_url` — every unit advertises one: the ops URL
    for a runner, the reachable-host gateway URL, or the OTLP ingress for a pure
    station), a fresh `up_since_at`, and a cleared `stopped_at` (coming back up
    un-stops THIS unit). The composed `machines` row is then rebuilt from the
    machine's non-stopped units so every reader sees the union — see
    `_recompute_machine_row`.

    Two callers, and the second is what makes the record mean what its readers
    assume. `ava start` registers at the tail of a supervised bring-up; the ops
    daemon registers at its OWN boot (`services/agent_ops/daemon.py`), once it is
    actually serving. A host can come back up without completing an `ava start`
    — an OS-scheduled autostart, a watchdog respawn, a rollout's restart leg —
    and before the daemon registered itself such a host kept a `stopped_at` latch
    nothing cleared while it served ops (the 2026-07-28 runner roster read
    `stopped` while its cron autostart had in fact brought it up). The process
    whose liveness the row stands for is now the process that writes it.

    `up_since_at` therefore means **the last time a process owning this unit
    announced the unit was up** — a boot/announce stamp, not a heartbeat: nothing
    refreshes it while the unit merely keeps running. It is display-only ("up
    since"); no decision path reads it, and liveness proper is the live
    `status_probe` (`MachineStatus.online`). The column was called `last_seen_at`
    until #981, which is a name for a fact nothing in this schema records.

    `last_seen_at` was the pre-rename name, dual-written through the expand
    window and dropped by the contract migration (20260811T050000).

    `url` is this unit's inbound base URL — see `unit_dial_url()`, which both
    callers use to resolve it. When `url` is None we do not fall back to
    `gateway_url()`; the caller is responsible for the URL.

    Args:
        url: this unit's inbound base URL (resolved by the caller per role).
            None → store NULL (a unit that advertises no address).

    Raises:
        MachineNameMissing: machine_name() is unset.
        MachineRoleMissing: machine_role() is unset.
    """
    name = machine_name()
    home = str(ava_home())
    serve_gateway = is_gateway()
    serve_agent_runner = is_agent_runner()
    serve_observability_station = is_observability_station()
    _reject_loopback_dial_url(
        url,
        serve_gateway=serve_gateway,
        serve_agent_runner=serve_agent_runner,
        serve_observability_station=serve_observability_station,
    )
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, "
            "serve_observability_station, url, up_since_at, stopped_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, NOW(), NULL) "
            "ON CONFLICT (machine_name, home) DO UPDATE SET "
            "serve_gateway = EXCLUDED.serve_gateway, "
            "serve_agent_runner = EXCLUDED.serve_agent_runner, "
            "serve_observability_station = EXCLUDED.serve_observability_station, "
            "url = EXCLUDED.url, up_since_at = NOW(), "
            # coming back up clears this unit's prior intentional-stop marker
            "stopped_at = NULL",
            (name, home, serve_gateway, serve_agent_runner, serve_observability_station, url),
        )
        _recompute_machine_row(cur, name)
        # description is host-level config; only register_self (running on the
        # host) writes it. recompute leaves it untouched, so stamp it here after
        # the composed row exists.
        cur.execute(
            "UPDATE machines SET description = %s WHERE name = %s",
            (machine_description(), name),
        )
        conn.commit()


def mark_stopping(name: str, home: str) -> None:
    """Stamp `stopped_at = NOW()` on the (name, home) UNIT, then recompute.

    Called by the `POST /api/cluster/stopping` handler when a unit announces it
    is shutting down on purpose (just before `ava stop` tears the stack down),
    so the cluster view can show "stopped" rather than "offline" (which a live
    probe cannot tell apart from a crash). The retract is per-unit: stamping
    one unit's row and recomputing drops only that unit's caps from the
    composed machines row, so a co-located peer keeps its capability (e.g. the
    agent-runner unit stopping leaves the host still `gateway`). `register_self()`
    clears the unit's marker on the next `ava start`.

    `home` is the stopping unit's $AVA_HOME, sent on the wire by the announcing
    unit — it cannot be inferred from this (gateway) process's `ava_home()`,
    because a co-located or remote unit announcing its own stop runs under a
    different home than the gateway handling the announce. No-op-safe: a missing
    unit row simply updates zero rows, and recompute leaves the composed machines
    row consistent.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET stopped_at = NOW() WHERE machine_name = %s AND home = %s",
            (name, home),
        )
        _recompute_machine_row(cur, name)
        conn.commit()


def _recompute_machine_row(cur: psycopg.Cursor, name: str) -> None:
    """Rebuild the `machines` row for `name` by composing its `machine_units`.

    Composition over the machine's NON-stopped units:
    - role = sorted union of caps ('gateway' if any live unit serves gateway,
      'agent-runner' if any serves agent-runner, 'observability-station' if any
      serves observability-station).
    - gateway_url = the dial URL: the live agent-runner unit's `url` (ops URL)
      when the machine serves agent-runner, else the gateway unit's `url`, else
      the station unit's advertised OTLP ingress URL. This keeps the single-box
      meaning where `machines.gateway_url` holds the ops URL for
      agent-runner-capable hosts.
    - up_since_at = max over live units; stopped_at = NULL if any live unit,
      else NOW() (every unit announced an intentional stop).
    - description is left untouched (host-level config written only by
      register_self; see its trailing UPDATE).

    When ALL units of a machine are stopped there is no live unit to dial: caps
    is empty and url is NULL, but the `machines` CHECK requires a non-empty role,
    so the row is left as-is (caps retained, stopped_at = NOW()) — a fully
    stopped host stays in the roster shown as "stopped", matching pre-split
    behavior. The row is only ever deleted explicitly via the decommission
    endpoint.

    `cur` is the caller's open cursor (same transaction as the unit UPSERT /
    stop stamp, so the read-then-write is atomic).
    """
    cur.execute(
        # up_since_at is the #981-renamed stamp; the expand-window COALESCE
        # (up_since_at, last_seen_at) was removed with the contract migration —
        # a unit whose host is still on pre-#981 code registers itself
        # with `last_seen_at` alone, leaving `up_since_at` NULL for that row, and
        # the max below would raise on the mix. Two units of one machine can sit
        # on different checkouts during a rollout, so this is reachable.
        "SELECT serve_gateway, serve_agent_runner, serve_observability_station, url, up_since_at "
        "FROM machine_units WHERE machine_name = %s AND stopped_at IS NULL",
        (name,),
    )
    live = cur.fetchall()

    serve_gateway = any(row[0] for row in live)
    serve_agent_runner = any(row[1] for row in live)
    serve_observability_station = any(row[2] for row in live)
    role = sorted(
        cap
        for cap, on in (
            ("gateway", serve_gateway),
            ("agent-runner", serve_agent_runner),
            ("observability-station", serve_observability_station),
        )
        if on
    )
    if not role:
        # No live unit — leave the existing composed row untouched but mark it
        # stopped (the machines CHECK forbids an empty role array).
        cur.execute("UPDATE machines SET stopped_at = NOW() WHERE name = %s", (name,))
        return

    # Dial URL: ops URL of the live agent-runner unit when present, else the
    # gateway unit's URL, else the station unit's advertised OTLP ingress URL
    # (WP4: a pure station advertises the address remote consumers dial — the
    # bearer-authenticated OTLP ingress; see
    # conventions/reachability-and-credentials.md).
    runner_url = next((row[3] for row in live if row[1]), None)
    gateway_only_url = next((row[3] for row in live if row[0]), None)
    station_only_url = next((row[3] for row in live if row[2]), None)
    gateway_url = (
        runner_url
        if serve_agent_runner
        else gateway_only_url
        if gateway_only_url is not None
        else station_only_url
    )
    # The composed "up since" is the LATEST announce across live units; a unit
    # whose up_since_at is NULL (pre-#981 registration, never backfilled)
    # contributes nothing to the max instead of raising.
    up_since_at = max((row[4] for row in live if row[4] is not None), default=None)

    # description is host-level config, written only by register_self (which runs
    # on the host itself). recompute does not touch it: a fresh row inserts NULL,
    # and a recompute triggered by stop (possibly handled by another host's
    # process) preserves the existing description rather than overwriting it with
    # the wrong host's value.
    cur.execute(
        # up_since_at is the #981-renamed stamp (last_seen_at dropped by the
        # contract migration — see register_self).
        "INSERT INTO machines (name, gateway_url, role, up_since_at, stopped_at) "
        "VALUES (%s, %s, %s, %s, NULL) "
        "ON CONFLICT (name) DO UPDATE SET gateway_url = EXCLUDED.gateway_url, "
        "role = EXCLUDED.role, up_since_at = EXCLUDED.up_since_at, "
        "stopped_at = NULL",
        (name, gateway_url, role, up_since_at),
    )


def lookup(name: str) -> str:
    """SELECT gateway_url FROM machines WHERE name=%s.

    Raises:
        MachineNotRegistered: no such row in machines (the other
            host has not yet run `ava start`, or the machine name is
            misspelled).
        MachineGatewayUrlMissing: row exists but gateway_url is NULL.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT gateway_url FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        raise MachineNotRegistered(
            f"no machine named {name!r} (it has not registered itself yet, or the name is "
            f"misspelled); see ava.agents.list_machines() for the valid names."
        )
    if row[0] is None:
        raise MachineGatewayUrlMissing(
            f"machine {name!r} exists in machines table but does not advertise a gateway_url."
        )
    return row[0]


def lookup_role(name: str) -> list[str]:
    """SELECT role FROM machines WHERE name=%s — the target's capability set.

    Used by the spawn router to reject a target that cannot run agents with a
    precise 400/404 instead of forwarding into an unreachable ops dial.

    Raises:
        MachineNotRegistered: no such row (the host has not run `ava start`, or
            the name is misspelled).
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT role FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        raise MachineNotRegistered(
            f"no machine named {name!r} (it has not registered itself yet, or the name is "
            f"misspelled); see ava.agents.list_machines() for the valid names."
        )
    return row[0]


def list_all() -> list[tuple[str, str | None]]:
    """SELECT name, gateway_url FROM machines — for `ava machines list` (future) / debug."""
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, gateway_url FROM machines ORDER BY name")
        return cur.fetchall()


def list_agent_runners() -> list[tuple[str, str | None]]:
    """SELECT (name, gateway_url) FROM machines WHERE role='agent-runner' AND not
    intentionally stopped — the fan-out target list for a cluster-wide `ava cluster update`.

    One query for single-box and split alike — no host-mode branch; machines are
    listed purely by capability. A single-box gateway,agent-runner host is
    included (it carries agent-runner). This is the
    single SELECT both `ava cluster update`'s fan-out (`cli/commands/update.py:
    _list_agent_runners`) and any future caller share, so the
    `'agent-runner' = ANY(role)` predicate lives in one place.

    Including the orchestrating host is right for this query and wrong for one of
    its consumers: the rollout keeps it in Phase 0's fetch and Phase A's pause
    (idempotent with the local leg) but drops it from the Phase-B fan-out, whose op
    is not (`cli/commands/_update_orchestration.py:_phase_b_targets`, issue #1151).
    The exclusion belongs there, at the phase that cannot tolerate it, not here —
    every other caller wants the full capability list.

    `stopped_at IS NOT NULL` rows are excluded: a host that announced an
    intentional stop is not a rollout target, and `register_self()` clears the
    marker when it returns (the watchdog catch-up then re-triggers its update). An
    offline host that crashed without deregistering stays in the list and is
    classified at dial time (unreachable = warn-and-skip in the fan-out, and the
    Phase-B poll only waits on hosts that acked) — not hidden by query scope.
    `is_staging` rows are excluded the same way: a staging host (operator-set
    flag, `ava cluster mark-staging`) is registered and roster-visible but never
    a rollout target — `ava start` on it clears its `stopped_at` like any host,
    and the staging flag is what keeps it out of the target set.
    `paused_at IS NOT NULL` rows are excluded the same way — the pause latch
    (`ava cluster pause`, migration 20260814T182039) is operator-set and NEVER
    cleared by `register_self`: a paused host that answers a probe is still not
    a rollout target, and the heartbeat liveness pass (the other caller of this
    query) skips it so an expected absence fires no offline alert. Only
    `ava cluster resume` clears the latch.
    `up_since_at` is a boot/announce stamp (`ava start`, and the ops daemon's own
    boot), not a heartbeat, so it cannot judge "online"; that determination is
    deferred to the live HTTP call.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, gateway_url FROM machines "
            "WHERE 'agent-runner' = ANY(role) AND stopped_at IS NULL "
            "AND NOT is_staging AND paused_at IS NULL ORDER BY name"
        )
        return cur.fetchall()


def list_stopped_agent_runners() -> list[tuple[str, str | None]]:
    """SELECT (name, gateway_url) FROM machines WHERE role='agent-runner' AND the
    row IS marked intentionally stopped — the exact complement of
    `list_agent_runners()` over the same capability predicate.

    Together the two cover every agent-runner-capable row, which is what lets a
    rollout state how many of how many KNOWN hosts it is targeting instead of
    printing a bare count of whatever survived the filter. The caller is expected
    to probe these rather than trust the marker: `stopped_at` is a latch — set
    only by the `ava stop` announce (`mark_stopping`) and cleared only by
    `register_self()` — with no relation to liveness. The ops daemon's boot
    registration closes the wide version of that gap (a host whose autostart or
    watchdog brought it back without a completed `ava start` now un-stops itself),
    but the latch can still outlive the condition: a peer unit's recompute can
    re-stamp the composed row, and a gateway-only unit runs no ops daemon to
    re-announce itself. Silently dropping a live host from a migration-carrying
    rollout leaves it writing the central DB on old code (the 2026-07-28
    runner rollout), so the probe stays the authority.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, gateway_url FROM machines "
            "WHERE 'agent-runner' = ANY(role) AND stopped_at IS NOT NULL "
            "AND NOT is_staging AND paused_at IS NULL ORDER BY name"
        )
        return cur.fetchall()


def set_staging(name: str, *, is_staging: bool) -> bool:
    """Set or clear the operator staging flag on a machine row; True when a row
    changed.

    The staging latch is operator-only (`ava cluster mark-staging` /
    `unmark-staging`, backed by this function): `register_self` and the ops
    daemon never write it, so a staging host that runs `ava start` stays
    excluded from `list_agent_runners()` even though its `stopped_at` latch is
    cleared. False when no row matched the name (no such machine).

    Args:
        name: the machines-table row to flag.
        is_staging: keyword-only; True marks the host staging (excluded from
            rollout fan-out);
            False restores it as a normal rollout target.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machines SET is_staging = %s WHERE name = %s",
            (is_staging, name),
        )
        return cur.rowcount > 0


def clear_stopped_marker(name: str) -> bool:
    """Clear the composed `machines` row's `stopped_at` for `name`; True if a row
    changed.

    The reconcile write for a stale stop latch. `stopped_at` on the composed row
    means "no unit of this machine is live", and a live probe answering at that
    row's own dial URL is direct evidence to the contrary — so the marker, not the
    probe, is what must give way.

    Only the composed row is cleared. The per-unit `machine_units.stopped_at`
    markers are left alone on purpose: the probe proves that SOME unit answers at
    the machine's dial URL, not which one, and blanket-clearing the unit rows would
    resurrect the capabilities of a peer unit that really is stopped. The live
    unit's next `register_self()` is what reconciles its own unit row; until then
    an unrelated recompute (a peer unit stopping) may legitimately re-stamp the
    composed row.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machines SET stopped_at = NULL WHERE name = %s AND stopped_at IS NOT NULL",
            (name,),
        )
        changed = cur.rowcount > 0
        conn.commit()
    return changed


def pause(name: str, reason: str | None = None) -> bool:
    """Set the operator pause latch on a machine row; True when a row changed.

    `paused_at = NOW()` + `pause_reason` on the composed `machines` row
    (migration 20260814T182039). The latch is operator-only — the gateway
    pause endpoint calls this AFTER it has terminated the machine's agents and
    drained its tasks; `register_self` and the ops daemon never write it, so a
    paused machine that runs `ava start` (or re-registers after its reachable
    address changes) stays paused until an explicit `ava cluster resume`.

    Consequences of the latch (each one a separate reader of this column):
    `list_agent_runners()` drops the row, so the heartbeat liveness pass does
    not probe it (no offline alert — an expected absence is not an incident)
    and the rollout fan-out skips it; the roster / cluster panel /
    `ava.agents.list_machines()` hide it; spawns targeting it are refused.

    The row itself is never deleted: `gateway_url` / `role` / `description`
    are the registration info `resume` needs, and a paused machine that comes
    back re-registers into the SAME row (its units' `stopped_at` latch clears;
    `paused_at` does not).

    Args:
        name: the machines-table row to pause.
        reason: free-text why (recorded for the resume checklist / audit).

    Returns:
        True when a row matched and was updated; False when no such machine.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machines SET paused_at = NOW(), pause_reason = %s "
            "WHERE name = %s AND paused_at IS NULL",
            (reason, name),
        )
        changed = cur.rowcount > 0
        conn.commit()
    return changed


def resume(name: str) -> bool:
    """Clear the operator pause latch on a machine row; True when a row changed.

    The machine becomes a normal cluster member again: the next heartbeat
    liveness pass probes it (a successful probe also resolves any lingering
    offline alert), the roster / cluster panel / agents' list_machines serve
    it, the rollout fan-out re-includes it and spawns are accepted again.
    The machine itself needs no action on the gateway side — when it is back
    online it re-runs `ava start`, whose `register_self()` clears its
    `stopped_at` latch and refreshes `gateway_url` (which may have changed
    with a new reachable address).

    Idempotent: resuming a machine that is not paused changes nothing and
    returns False (the CLI reports it as a no-op, not an error).

    Args:
        name: the machines-table row to resume.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machines SET paused_at = NULL, pause_reason = NULL "
            "WHERE name = %s AND paused_at IS NOT NULL",
            (name,),
        )
        changed = cur.rowcount > 0
        conn.commit()
    return changed


def is_paused(name: str) -> bool:
    """True when `name`'s machines row carries the operator pause latch.

    Used by the spawn preflight to refuse a paused spawn target with a precise
    409 (`MachinePaused`) instead of forwarding into an unreachable ops dial.
    A missing row raises `MachineNotRegistered` like `lookup_role` — callers
    that already resolved the role have the row's existence established.

    Raises:
        MachineNotRegistered: no such row.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT paused_at IS NOT NULL FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        raise MachineNotRegistered(
            f"no machine named {name!r} (it has not registered itself yet, or the name is "
            f"misspelled); see ava.agents.list_machines() for the valid names."
        )
    return row[0]


def list_paused() -> list[tuple[str, str | None]]:
    """SELECT (name, gateway_url) FROM machines WHERE paused_at IS NOT NULL —
    the operator's view of the paused set, ordered by name.

    The roster and every agent-facing view hide paused rows by design (the
    cluster shows only active members), so this is the one query that
    enumerates them — for the pause/resume CLI output and for tests proving
    the state machine. The pause latch, not a probe, is the authority here:
    a paused machine is expected to be unreachable, so probing it would
    answer nothing useful.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, gateway_url FROM machines WHERE paused_at IS NOT NULL ORDER BY name"
        )
        return cur.fetchall()
