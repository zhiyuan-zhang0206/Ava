"""Settings-free `ava start` preflight — the installed-home gate.

`ava start` is a pure bring-up (identity is the home path; birth happens at
install). This gate refuses a home the registry does not corroborate — never
installed, or claiming a port block the registry allocated to someone else —
with a role-appropriate pointer instead of birthing anything. It must run BEFORE
any `cli.commands` import, because that package import instantiates Settings,
which on an uninstalled home fails with a generic validation error instead of
the actionable message. Mirrors `cli.enroll`'s settings-free posture: stdlib +
`shared.dotenv_boot` + dotenv file reads only.

Ordering: `cli.main` runs this gate before the settings-loading command
imports. An enrolled runner's `.env` (written by `ava enroll`) carries the
bootstrap env — gateway URL + identity — and the cluster's connection facts
arrive at Settings build via the gateway fetch, so the gate checks only that
the runner was enrolled, never for cached connection facts (the 2026-08-01
config refactor deleted the `.env` materialization).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from dotenv import dotenv_values

from shared.port_block import LEGACY_AVA_PORTS, PORT_OFFSETS

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _serve_flag(
    file_name: str, env_key: str, home: Path, env_vals: dict[str, str | None]
) -> bool | None:
    """Settings-free mirror of one capability flag's resolution: process env >
    the home's `.env` value (what the env is loaded FROM at boot) >
    `$AVA_HOME/<file>` > None. A malformed value reads as None here — the real
    resolver raises loudly on it later; the gate only needs the role shape."""
    raw: str | None = os.environ.get(env_key) or env_vals.get(env_key)
    if raw is None or not raw.strip():
        p = home / file_name
        raw = p.read_text() if p.exists() else None
    if raw is None or not raw.strip():
        return None
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _home_record(home: Path, env_vals: dict[str, str | None]) -> dict[str, object] | None:
    """The host registry's record for `home`, or None — settings-free read of
    `clusters.json` (env/.env `AVA_CLUSTER_REGISTRY` > the default path),
    tolerating both the home-keyed and the legacy name-keyed file shape by
    matching each record's own `gateway_home`. A corrupt registry raises — the
    gate must not read "unparseable" as "not installed"."""
    reg = (
        os.environ.get("AVA_CLUSTER_REGISTRY")
        or env_vals.get("AVA_CLUSTER_REGISTRY")
        or "~/.ava/clusters.json"
    )
    p = Path(reg).expanduser()
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    key = str(Path(home).expanduser())
    return next((rec for rec in raw.values() if str(rec.get("gateway_home", "")) == key), None)


def _env_port_block(env_vals: dict[str, str | None]) -> dict[str, int]:
    """The ports this home's `.env` actually claims, keyed like a registry
    record's `ports`.

    Three anchors, because they are the ones that would collide with another
    cluster's data plane: the outward gateway port, and the Postgres / Redis
    ports carried inside the connection URLs (names-as-data — the port is read
    out of the URL, never re-derived). A key is absent when the `.env` does not
    name it or the value is not a port; absent means "claims nothing", never
    "claims 0"."""
    block: dict[str, int] = {}
    raw = (env_vals.get("AVA_GATEWAY_PORT") or "").strip()
    if raw.isdigit():
        block["gateway"] = int(raw)
    for name, url_key in (("postgres", "AVA_DB_URL"), ("redis", "AVA_REDIS_URL")):
        url = (env_vals.get(url_key) or "").strip()
        if not url:
            continue
        try:
            port = urlsplit(url).port
        except ValueError:
            continue  # unparseable port — Settings will reject it far more loudly
        if port is not None:
            block[name] = port
    return block


def _record_pgbouncer_port(rec: dict[str, object], rec_ports: dict[str, object]) -> int | None:
    """This record's PgBouncer listener port, settings-free.

    Mirror of `shared.cluster.record_pgbouncer_port` (the gate must not import
    shared.cluster — it loads Settings): the saved `pgbouncer` key wins; a
    record saved before the slot existed derives the fixed legacy 6433 for the
    default home, else its block base + the pgbouncer offset. The pooler port
    is part of this cluster's OWN block, so AVA_DB_URL legitimately carries it
    whenever pooling is enabled (the one-URL design)."""
    pgb = rec_ports.get("pgbouncer")
    if isinstance(pgb, int):
        return pgb
    gw = rec_ports.get("gateway")
    if not isinstance(gw, int):
        return None
    home = str(Path(str(rec.get("gateway_home", ""))).expanduser())
    if home == str((Path.home() / ".ava").expanduser()):
        return LEGACY_AVA_PORTS["pgbouncer"]
    return gw + PORT_OFFSETS["pgbouncer"]


def _port_block_conflicts(rec: dict[str, object], env_vals: dict[str, str | None]) -> list[str]:
    """Ports where the home's `.env` and its registry record disagree.

    The registry is the only thing that makes port ownership true, and it is not
    the thing a starting process reads — it reads `.env`. So a home whose `.env`
    outlived its record's port block (a destroy freed the block and a later birth
    took it, a hand edit, a registry restored from an older snapshot) would bring
    its data plane up on ports the registry has since promised to a live cluster.
    Comparing the two at the gate is what keeps home-directory isolation true.

    Only ports named on BOTH sides are compared: a record predating a port or an
    `.env` that does not name one is not drift. AVA_DB_URL may carry the record's
    Postgres port OR its PgBouncer port — both belong to this cluster's block,
    and which one the URL carries depends on AVA_PGBOUNCER_ENABLED at URL
    generation.
    """
    rec_ports = rec.get("ports")
    if not isinstance(rec_ports, dict):
        return []
    allocated_ports = cast("dict[str, object]", rec_ports)
    pgbouncer_port = _record_pgbouncer_port(rec, allocated_ports)
    conflicts: list[str] = []
    for name, claimed in sorted(_env_port_block(env_vals).items()):
        allocated = allocated_ports.get(name)
        if isinstance(allocated, int) and allocated != claimed:
            # The pooled URL legitimately names the pooler, not Postgres.
            if name == "postgres" and claimed == pgbouncer_port:
                continue
            conflicts.append(f"{name}: .env says {claimed}, the registry allocated {allocated}")
    return conflicts


def require_anchored_home(verb: str) -> int | None:
    """Refuse a verb that acts on this checkout's own cluster when the checkout
    claims none. Returns None to proceed, an error rc to refuse.

    `resolve_ava_home`'s last rule resolves a checkout with no `AVA_HOME`, no
    prod-source match and no `.ava_home` pointer to the DEFAULT home — production —
    flagged `anchored=False`. As a read fallback that is deliberate. But a verb that
    stops, restarts or reconfigures "this cluster" then aims at **production** from a
    dev worktree that owns no cluster at all, and nothing downstream can tell the
    difference: the pidfiles, the pg data dir and the `.env` it reads are all the real
    ones. `ava start` has been gated on this since it was written; every other verb in
    that family was not.

    Only the ANCHORING is checked here, deliberately — not the registry record or the
    port block `require_installed_home` also demands. Those answer "may this home bring
    a data plane UP on these ports", which is a start-time question; a home whose
    record was destroyed must still be able to stop itself and clean up.
    """
    from shared.dotenv_boot import resolve_ava_home

    home, anchored = resolve_ava_home()
    if anchored:
        return None
    print(
        f"✗ ava {verb}: this checkout claims no cluster (no AVA_HOME, not the prod "
        f"source, no .ava_home pointer), so it resolves to the default home {home} — "
        f"`ava {verb}` from here would act on THAT cluster, not on this checkout. "
        "Birth this checkout's own cluster first:\n"
        "  scripts/install.sh --worktree   # from this checkout\n"
        f"To act on {home} deliberately, run ITS `ava` (the one on PATH), not this "
        "checkout's.",
        file=sys.stderr,
    )
    return 1


def require_installed_home() -> int | None:
    """Refuse `ava start` for a home that was never installed, pointing at the
    right birth path for this host's role.

    Returns None when the home is installed (proceed); an error rc otherwise.
    Discrimination:
    - unanchored checkout (no AVA_HOME / prod-source match / `.ava_home`
      pointer): a dev worktree that never ran the install — point at
      `scripts/install.sh --worktree`.
    - anchored, pure agent-runner: its enrolled `.env` must exist AND carry
      AVA_GATEWAY_URL (the marker of a completed enroll; connection facts are
      fetched from that gateway at every start, never cached in `.env`) — else
      point at `ava enroll`.
    - anchored, gateway-capable (or role unknown), no registry record for the
      home: point at `scripts/install.sh --role ...`.
    - anchored, registered, but the home's `.env` claims ports its record did not
      allocate: refuse rather than bring a data plane up on someone else's block.
    """
    from shared.dotenv_boot import resolve_ava_home

    rc = require_anchored_home("start")
    if rc is not None:
        return rc
    home, _anchored = resolve_ava_home()

    env_path = home / ".env"
    env_vals: dict[str, str | None] = dotenv_values(env_path) if env_path.exists() else {}
    serve_gateway = _serve_flag(
        "machine_serve_gateway", "AVA_MACHINE_SERVE_GATEWAY", home, env_vals
    )
    serve_runner = _serve_flag(
        "machine_serve_agent_runner", "AVA_MACHINE_SERVE_AGENT_RUNNER", home, env_vals
    )
    runner_only = serve_runner is True and serve_gateway is not True
    if runner_only:
        if (env_vals.get("AVA_GATEWAY_URL") or "").strip():
            return None
        print(
            f"✗ ava start: {env_path} carries no gateway URL (no AVA_GATEWAY_URL) "
            "— this agent-runner was never enrolled, or the enroll did not complete. Run:\n"
            "  ava enroll --gateway <url> --machine-name <name> --machine-host "
            "<this-host-addr> with AVA_CLUSTER_SECRET set from a non-echoing prompt",
            file=sys.stderr,
        )
        return 1

    rec = _home_record(home, env_vals)
    if rec is None:
        print(
            f"✗ ava start: home {home} has no cluster (not in the registry) — `ava start` "
            "is a pure bring-up; install births the cluster. Run:\n"
            "  scripts/install.sh --role gateway,agent-runner   # prod host, from $AVA_HOME/source\n"
            "  scripts/install.sh --worktree                    # dev worktree, from its checkout",
            file=sys.stderr,
        )
        return 1

    conflicts = _port_block_conflicts(rec, env_vals)
    if conflicts:
        print(
            f"✗ ava start: home {home} would bind ports its registry record does not own:\n"
            + "".join(f"    {c}\n" for c in conflicts)
            + "  The registry is what makes port ownership true, and another cluster may "
            "already hold the ports\n  this .env names. Re-run the install for this home to "
            "re-derive .env from the record:\n"
            "  scripts/install.sh --worktree --path "
            f"{home}   # or --role ... for a prod/gateway home",
            file=sys.stderr,
        )
        return 1
    return None


def unit_already_stopped() -> bool:
    """Allow an idempotent cold stop without fetching the offline gateway."""
    from shared.dotenv_boot import resolve_ava_home
    from shared.pause_owner import read_for_home

    home, anchored = resolve_ava_home()
    if not anchored:
        return False
    current = read_for_home(home)
    return (
        current.status == "paused"
        and current.maintenance is not None
        and current.maintenance.phase == "stopped"
        and not current.maintenance.failures
    )
