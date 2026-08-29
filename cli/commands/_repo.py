"""Path / role helpers + preflight checks used by every cmd_*.

The cli-facing façade for the service roster: `ServiceSpec`, `build_services()`,
and the capability filters (`_services_for_roles`, `_services_for_roles_annotated`)
are re-exported from `ops.spec` (the single desired-state source) so existing call
sites keep their `from cli.commands._repo import ...` imports. This module owns:
- `session_name()` composer re-export: `ava-<service>`
- `_roles_or_none()` capability read
- repo root + memory-pool / frontend / schema / gateway / register
  preflight checks reused by `start`, `status`, `update`, `cluster`, etc.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

# The service roster + capability filtering now live in `ops.spec` — the single
# desired-state source (`shared < ops < {gateway, cli}`). Bound here under their
# historical names (module-level assignments, so both ruff and pyright see them as
# intentional exports) so existing `from cli.commands._repo import ...` call sites
# (start / status / stop / update / converge / __init__) keep working; `_repo` stays
# the cli-facing façade, `ops.spec` owns the definitions.
from cli.commands._setup import SetupValues
from ops import spec as _spec

# Re-exported (redundant alias marks intentional re-export) so existing call
# sites `from cli.commands._repo import session_name` keep working after the
# composer moved to shared.cluster.
from shared.cluster import session_name as session_name
from shared.config import settings
from shared.deploy_timing import GATEWAY_PREFLIGHT_BUDGET_S
from shared.machine import MachineRoles
from shared.platform_backend import get_backend

ServiceSpec = _spec.ServiceSpec
build_services = _spec.build_services
profile_marker = _spec.profile_marker
_services_for_roles = _spec.services_for_capabilities
_services_for_roles_annotated = _spec.services_for_capabilities_annotated


def _roles_or_none() -> MachineRoles | None:
    """Read the capability set; on missing or invalid value return None — the
    explicit "this host's role is not resolvable yet" state, distinct from a
    (never-valid) empty capability set. stop/status/converge should not be
    blocked by unfinished setup, so they treat None conservatively."""
    from shared.machine import MachineRoleInvalid, MachineRoleMissing, machine_role

    try:
        return machine_role()
    except (MachineRoleMissing, MachineRoleInvalid):
        return None


def _repo_root() -> Path:
    """cli/main.py -> cli/ -> repo root.

    cli/commands/ is one level deeper than the old cli/commands.py, so
    parents[2] from this file lands at the same repo root.
    """
    return Path(__file__).resolve().parents[2]


def _ensure_frontend_deps(repo: Path) -> None:
    """Install frontend deps when node_modules is missing OR when
    package-lock.json changed since the last install — `npm run build` without
    the exact locked deps dies immediately and the session exits (hit by
    first-time install, fresh clone, AND by `ava cluster update` pulling a lockfile that
    adds a dependency: build fails, `npm run build && exec npm run start` short-
    circuits, port 3000 goes dark).

    `node_modules/.ava-lock-hash` stamps the sha256 of the package-lock.json we
    last installed from; a missing/mismatched stamp means node_modules is stale
    against the current lockfile. Written *after* `npm ci` because `npm ci`
    wipes node_modules first.
    """
    fe = repo / "ui" / "web"
    node_modules = fe / "node_modules"
    stamp = node_modules / ".ava-lock-hash"
    want = hashlib.sha256((fe / "package-lock.json").read_bytes()).hexdigest()
    if node_modules.is_dir() and stamp.is_file() and stamp.read_text().strip() == want:
        return
    reason = "missing" if not node_modules.is_dir() else "package-lock.json changed"
    print(f"  · frontend deps {reason}, running npm ci (~30-60s)")
    # On Windows `npm` is `npm.cmd`, which CreateProcess won't resolve from a bare
    # "npm" argv — run it through the shell so the .cmd shim is found.
    subprocess.run(["npm", "ci"], cwd=str(fe), check=True, shell=get_backend().npm_shell_flag())
    stamp.write_text(want)


def _assert_schema_current_or_die() -> int:
    """Verify the DB's applied migration set == the code's required set. Targeted
    hints for the two failure shapes (DB behind code / code behind DB)."""
    from shared.migrations import (
        CodeBehindSchema,
        SchemaVersionMismatch,
        assert_schema_current,
    )

    try:
        assert_schema_current(settings.data_plane.db_url)
    except SchemaVersionMismatch as e:
        print(
            f"  ✗ {e}\n"
            f"    DB is behind the code in this checkout — the apply-migrations step of `ava start` "
            f"should have caught up just before this check, so this likely means migrations/ in this "
            f"checkout was edited mid-flight.",
            file=sys.stderr,
        )
        return 1
    except CodeBehindSchema as e:
        print(
            f"  ✗ {e}\n"
            f"    The central DB is ahead of this checkout — typically the gateway ran `ava cluster update` while "
            f"this host stayed on an older revision. Run `git pull && uv sync` on this host, then retry "
            f"`ava start`.",
            file=sys.stderr,
        )
        return 1
    print("  ✓ schema version matches code")
    return 0


# The one endpoint that answers "can a host use the gateway?". Authenticated, and
# deliberately **exempt from the paused-host 503 middleware**
# (`gateway/routers/cluster.py`), so a 200 here means the gateway is *serving* — not
# that the cluster is unpaused. It is also served only after `gateway.app.main`'s
# `assert_schema_current` has passed, so serving implies migrated: the two are not
# separate instants a caller has to wait for in turn.
GATEWAY_PROBE_PATH = "/api/cluster/status"


class GatewayProbe(NamedTuple):
    """One dial of the gateway's probe endpoint, classified but not judged.

    `status` is the HTTP status, or None when the dial got no answer at all
    (connection refused / timeout — nothing is listening). `detail` is the exception
    text in that case, else the truncated response body.

    Shared between the agent-runner's own preflight (`_probe_gateway_or_die`) and the
    rollout's readiness gate (`cli.commands._gateway_ready`): that is what makes the
    gate's success criterion *the same criterion* the preflight applies seconds later,
    rather than a second definition of "reachable" that merely tends to agree with it.
    """

    status: int | None
    detail: str


def probe_gateway_once(gateway_url: str, *, timeout_s: float = 10.0) -> GatewayProbe:
    """Dial `GATEWAY_PROBE_PATH` on `gateway_url` once, with this cluster's auth.

    Classifies rather than decides: no printing, no retrying, no exit code. The
    callers differ entirely in what they do with a non-answer (the preflight gives up,
    the readiness gate keeps waiting), and only in that.
    """
    import httpx

    from shared.http_dial import get as dial_get
    from shared.machine import gateway_auth_headers

    try:
        resp = dial_get(
            f"{gateway_url.rstrip('/')}{GATEWAY_PROBE_PATH}",
            timeout=timeout_s,
            headers=gateway_auth_headers(),
        )
    except httpx.HTTPError as e:
        return GatewayProbe(None, str(e))
    return GatewayProbe(resp.status_code, resp.text[:200])


# Gap between preflight dials. Small relative to the budget so a hole a few seconds
# wide is noticed a few seconds after it closes rather than at the next round number,
# and large enough that a genuinely dead gateway is reported in a handful of lines
# instead of a wall of them.
_PREFLIGHT_RETRY_INTERVAL_S = 5.0


def _probe_gateway_or_die(gateway_url: str, *, budget_s: float = GATEWAY_PREFLIGHT_BUDGET_S) -> int:
    """Probe the gateway from an agent-runner over the private network.

    The agent-runner's ops server is dialed *by* the gateway, but the host
    still reaches the gateway for watchdog self-heal updates and
    cluster status. Probe at `ava start` time so a broken private-network path
    fails on stdout instead of only surfacing later during a self-heal.

    **Both transient shapes get the same bounded budget**: a 5xx (the gateway
    answered but is not ready) and no answer at all (nothing is listening on that
    address *yet*). They used to be treated as opposites — the 5xx retried, a refused
    connection failed on the first dial — on the reasoning that a rollout must not
    paper over an ordering bug here, since making the gateway ready before any runner
    is told to update is the orchestrator's job (`cli.commands._gateway_ready`). That
    reasoning stands and the gate still owns the ordering; what it does not cover is a
    gateway that was serving when the gate probed it and is briefly not by the time
    this dial lands. Prod produced exactly that on 2026-08-01 (issue #1151): a ~9 s
    restart hole, one ECONNREFUSED, an immediate decline, and two runners stranded
    until the settle lease lapsed 15 minutes later. A single refused packet is not
    evidence that the gateway is down, and treating it as such trades a 30 s wait for
    a 15 minute one.

    The budget buys nothing on the healthy path: a reachable gateway answers on the
    first dial and returns immediately. A gateway that is genuinely down still fails —
    `budget_s` is deliberately too short to outlast a real death, which needs the
    watchdog's own round to fix (`shared.deploy_timing.GATEWAY_PREFLIGHT_BUDGET_S`).

    A non-200 the gateway *chose* to send (401/403/404) is terminal on the first dial
    as before: a credential or route mismatch is not a timing problem.
    """
    deadline = time.monotonic() + budget_s
    attempt = 0
    while True:
        attempt += 1
        probe = probe_gateway_once(gateway_url)
        if probe.status == 200:
            print(f"  ✓ gateway reachable ({gateway_url})")
            return 0
        if probe.status is not None and probe.status < 500:
            print(
                f"  ✗ unexpected status {probe.status} from gateway: {probe.detail}.",
                file=sys.stderr,
            )
            return 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if probe.status is None:
                print(
                    f"  ✗ gateway unreachable at {gateway_url} after {attempt} attempt(s) "
                    f"over {budget_s:.0f}s: {probe.detail}.\n"
                    f"    Check `AVA_GATEWAY_URL` in ~/.ava/.env and that this host can "
                    f"reach the gateway over the cluster's private network.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ gateway returned {probe.status} at {GATEWAY_PROBE_PATH} after "
                    f"{attempt} attempt(s) over {budget_s:.0f}s: {probe.detail}.\n"
                    f"    Gateway may be mid-restart; retry later.",
                    file=sys.stderr,
                )
            return 1
        wait_s = min(_PREFLIGHT_RETRY_INTERVAL_S, remaining)
        shape = "unreachable" if probe.status is None else f"returned {probe.status}"
        print(
            f"  ⚠ gateway {shape} (attempt {attempt}, {remaining:.0f}s of budget left); "
            f"retrying in {wait_s:.0f}s…",
            file=sys.stderr,
        )
        time.sleep(wait_s)


def _register_machine_or_die(resolved: SetupValues, roles: MachineRoles) -> int:
    """UPSERT this host into the machines table with typed error handling.

    The dial URL comes from `shared.machines.unit_dial_url(roles)` — the one
    definition, shared with the ops daemon's boot registration so the two writers
    of this row cannot advertise different addresses for the same unit.

    Failure modes get targeted hints + non-zero exit (a half-registered host
    silently breaks cross-machine orchestration; we'd rather fail loud on
    `ava start`). The ops daemon's equivalent call is deliberately non-fatal
    instead — see `services/agent_ops/daemon.py:_register_boot`.
    """
    import psycopg

    from shared.machines import LoopbackDialUrlRefused, register_self, unit_dial_url

    url = unit_dial_url(roles)
    try:
        register_self(url=url)
    except LoopbackDialUrlRefused as e:
        print(
            f"  ✗ {e}",
            file=sys.stderr,
        )
        return 1
    except psycopg.OperationalError as e:
        print(
            f"  ✗ register_self failed: cannot connect to central Postgres ({e}).\n"
            f"    Check `AVA_DB_URL` in ~/.ava/.env points at the gateway's reachable Postgres "
            f"(public TLS endpoint or LAN). On agent-runner, also verify CF Tunnel / private "
            f"network connectivity to the gateway host.",
            file=sys.stderr,
        )
        return 1
    except psycopg.errors.UndefinedTable as e:
        print(
            f"  ✗ register_self failed: `machines` table does not exist ({e}).\n"
            f"    Gateway's DB schema is behind — run `ava cluster update` (or `ava start`, which "
            f"applies pending migrations) on the gateway, then retry `ava start` here.",
            file=sys.stderr,
        )
        return 1
    print(f"  ✓ {resolved['machine_name']} → {url or '(no dial url — station-only)'}")
    return 0


def _preflight_probes() -> int:
    """Run gateway and DB reachability checks BEFORE stopping services.

    Designed for `ava restart` and `ava cluster update` (self-update leg): validate
    that the host can still reach the gateway *before* killing its own
    services, so a transient gateway outage or network blip does not leave
    the host in a "services dead, can't start" state.

    Resolves the host's setup (gateway URL, machine name, roles) from the
    same persisted config that `_cmd_start_body` uses, then runs the same
    register + probe checks that `_cmd_start_body` runs *after* the stop.
    On failure the host keeps serving — the caller aborts without stopping.

    Returns 0 when both checks pass, non-zero otherwise.
    """
    import cli.commands as _ns
    from cli.commands._setup import _collect_setup_values, _print_missing_setup_error

    # Resolve setup from persisted env/files (all None args = read-only, no writes).
    args: dict[str, str | bool | None] = {
        "machine_name": None,
        "machine_serve_gateway": None,
        "machine_serve_agent_runner": None,
        "machine_serve_observability_station": None,
        "machine_description": None,
        "memory_remote": None,
        "gateway_url": None,
    }
    resolved, missing = _collect_setup_values(args)
    if missing:
        _print_missing_setup_error(missing, resolved.get("machine_role"))
        return 1

    roles_raw = resolved.get("machine_role", "")
    roles: MachineRoles = frozenset(roles_raw.split(",")) if roles_raw else frozenset()

    print("\n→ preflight: register machine in central DB")
    rc = _ns._register_machine_or_die(resolved, roles)
    if rc != 0:
        print("  ✗ preflight failed: cannot register machine — host still serving", file=sys.stderr)
        return rc

    if "agent-runner" in roles and "gateway" not in roles:
        print("\n→ preflight: probe gateway")
        rc = _ns._probe_gateway_or_die(resolved["gateway_url"])
        if rc != 0:
            print("  ✗ preflight failed: gateway unreachable — host still serving", file=sys.stderr)
            return rc

    return 0
