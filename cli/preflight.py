"""Settings-free home anchoring and reservation validation for lifecycle commands.

First start publishes identity through cli.start_intent before Settings loads.
Other lifecycle commands require an existing checkout or explicit home anchor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from base.host.env.port_block import LEGACY_AVA_PORTS


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
            from base.host.net.predicates import is_loopback_host

            parts = urlsplit(url)
            host = parts.hostname or ""
            own_host = (env_vals.get("AVA_MACHINE_HOST") or "").lower().strip("[]")
            if host and not is_loopback_host(host) and host != own_host:
                continue  # Foreign resources do not claim this host's port block.
            port = parts.port
        except ValueError:
            continue  # unparseable port — Settings will reject it far more loudly
        if port is not None:
            block[name] = port
    return block


def _record_pgbouncer_port(rec: dict[str, object], rec_ports: dict[str, object]) -> int | None:
    """This record's PgBouncer listener port, settings-free.

    Mirror of `base.cluster.record_pgbouncer_port` (the gate must not import
    runtime Settings): the saved `pgbouncer` key wins; a
    record for the default home may fall back to the fixed legacy 6433. The
    pooler port is part of this cluster's OWN block, so AVA_DB_URL legitimately
    carries it whenever pooling is enabled (the one-URL design)."""
    pgb = rec_ports.get("pgbouncer")
    if isinstance(pgb, int):
        return pgb
    home = str(Path(str(rec.get("gateway_home", ""))).expanduser())
    if home == str((Path.home() / ".ava").expanduser()):
        return LEGACY_AVA_PORTS["pgbouncer"]
    return None


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
    prod-source match and no `.ava_home` pointer to a private per-process scratch
    home, flagged `anchored=False`: it boots bare so tools and hooks keep working,
    but it owns no cluster. A verb that stops, restarts or reconfigures "this
    cluster" has nothing to act on there, and the default home it might have meant
    belongs to the prod source's own `ava`. So the family refuses with the birth
    command instead of quietly operating an empty scratch.

    This validates only the anchor. First-start identity owns reservation and
    port validation; stop must remain available to finish exact cleanup after
    a failed initialization or a recorded destroy intent.
    """
    from base.host.env.dotenv_boot import resolve_ava_home

    home, anchored = resolve_ava_home()
    if anchored:
        return None
    print(
        f"✗ ava {verb}: this checkout claims no cluster (no AVA_HOME, not the prod "
        f"source, no .ava_home pointer), so it runs on a throwaway scratch home {home} "
        f"— `ava {verb}` from here has no cluster to act on. "
        "Birth this checkout's own cluster first:\n"
        "  ava start --worktree   # from this checkout\n"
        "To act on the default home (~/.ava) deliberately, run ITS `ava` (the one on "
        "PATH), not this checkout's.",
        file=sys.stderr,
    )
    return 1


def unit_already_stopped() -> bool:
    """Allow an idempotent cold stop without fetching the offline gateway."""
    from base.deploy.maintenance.pause_owner import read_for_home
    from base.host.env.dotenv_boot import resolve_ava_home

    home, anchored = resolve_ava_home()
    if not anchored:
        return False
    current = read_for_home(home)
    return (
        current.status == "paused"
        and current.maintenance is not None
        and current.maintenance.phase == "stopped"
        and not current.maintenance.unsettled_failures()
    )
