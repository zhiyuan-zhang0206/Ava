#!/usr/bin/env python3
"""Rotate this cluster's `AVA_CLUSTER_SECRET` end-to-end.

Runs on the gateway box, against ITS OWN cluster (checkout-anchored `settings`,
like every other `cli`/`scripts` entry point — there is no `--home` flag). Reuses
the same idempotent "ensure" primitives `ava start` already calls to (re)affirm
the secret on every bring-up (`shared.cluster.ensure_cluster_role`,
`shared.cluster.ensure_cluster_redis_acl`, `cli.commands._pgbouncer.ensure_pgbouncer`)
— rotation is just calling them with a NEW secret instead of the current one, so
none of the auth machinery is reinvented here.

**`--dry-run` is the default** (no `--execute` flag = read-only: prints the plan,
the current identity/ports/pgbouncer posture, the enrolled-agent-runner roster
(names + URLs only, read from the `machines` table), and runs preflight probes
that confirm the CURRENT secret already authenticates everywhere. Nothing is
minted or written.

Sequence when `--execute` is passed (after an interactive `rotate` confirmation,
or `--yes` to skip it):

1. Mint a new secret (`secrets.token_urlsafe` — its alphabet is a strict subset of
   `DataPlaneSettings._validate_cluster_secret`'s allowed charset, so it always
   validates).
2. Postgres: `ensure_cluster_role` re-affirms the role's password over the
   loopback trust socket (`pg_admin_url`) — no restart, existing pooled
   connections are untouched (Postgres doesn't kick sessions on a password
   change; only a NEW connection needs the new password).
3. Redis: `ensure_cluster_redis_acl` re-affirms the cluster's ACL user, then
   `CONFIG SET requirepass` flips the `default` admin password — both live, no
   redis-server restart (this cluster's redis runs `--save ""`, so a restart
   would be a real, avoidable outage of anything durable in it).
4. PgBouncer (when `AVA_PGBOUNCER_ENABLED`): `ensure_pgbouncer` rewrites
   `userlist.txt` and SIGHUP-reloads — same online-reload path `ava start`
   already uses for a port/secret change.
5. Verify: reconnect to each with the NEW secret (must succeed) and the OLD
   secret (must now fail) — a rotation that silently no-ops is worse than one
   that fails loud.
6. Rewrite this gateway's own `.env` (`AVA_CLUSTER_SECRET` + the password inside
   `AVA_DB_URL`/`AVA_REDIS_URL`, via the same `url_with_password` the Settings
   loader itself re-applies on every boot) — `upsert_env` snapshots the old
   `.env` first, so it is never the only surviving copy.

What this script does **NOT** do (left to the runbook / the operator, by
design — narrow blast radius, no new service-lifecycle coupling here):

- **Restart the gateway process.** Existing connections survive steps 2-4
  untouched; the gateway's *in-memory* Settings only pick up the new secret on
  its next boot. Bounce it once step 6 has landed.
- **Push anything to already-enrolled agent-runners.** Since the 2026-08-01
  config refactor a runner's .env holds no cluster facts at all — it fetches
  them from the gateway at every process start — but its own
  `AVA_CLUSTER_SECRET` (the bearer for that very fetch) is deliberately NOT
  re-pullable: a runner cannot safely rotate the credential that gates the
  endpoint it would rotate it through. Each already-enrolled runner's
  `AVA_CLUSTER_SECRET` is an out-of-band credential.
  Each already-enrolled runner needs its `.env` `AVA_CLUSTER_SECRET` hand-edited
  (or the new value exported as `AVA_CLUSTER_SECRET` for a re-`ava enroll`, once
  the gateway itself already expects it) and then restarted. This script prints the roster
  (name + URL, from the `machines` table) as a checklist; it never dials them.
- **Provider API keys** (`ANTHROPIC_API_KEY` etc.) — those rotate through each
  provider's own console; this script only ever touches `AVA_CLUSTER_SECRET`.

Fail-closed: any exception during `--execute` writes a JSON recovery state file
(`$AVA_HOME/backups/secret-rotation/rotate-<timestamp>.json`, `0600` — it holds
both the old and the new secret in plaintext, matching how `.env` itself already
carries this secret) recording exactly which phase completed, and prints the
`--resume` command to re-run it. Every phase re-applies its target state
idempotently (same "ensure" primitives `ava start` calls on every bring-up), so
resuming — or re-running the whole thing twice — is always safe.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from cli.commands._cluster_instance import pg_admin_url
from shared.cluster import ensure_cluster_redis_acl, ensure_cluster_role, identity_from_url
from shared.config import settings
from shared.dotenv_boot import AVA_ENV_PATH
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.url_secret import url_with_password

# secrets.token_urlsafe's alphabet (A-Za-z0-9_-) is a strict subset of this —
# mirrored here only as a comment-level cross-check, not re-validated at
# runtime (DataPlaneSettings itself validates on the next Settings load).
_MINTED_SECRET_BYTES = 32


@dataclass
class RotationState:
    """Everything a mid-rotation failure needs to recover or resume. Every field
    is read straight off this process's own `settings.data_plane` at
    `build_state()` time — nothing is guessed or re-derived from a name."""

    identity: str
    old_secret: str
    new_secret: str
    pg_port: int
    redis_port: int
    pgbouncer_enabled: bool
    pgbouncer_port: int
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    phase: str = "minted"

    def path(self) -> Path:
        slug = self.started_at.replace(":", "").replace("+00:00", "Z")
        return ava_home() / "backups" / "secret-rotation" / f"rotate-{slug}.json"

    def save(self) -> Path:
        """Write this state to disk, 0600, overwriting any prior save for this
        same rotation attempt (same `started_at` -> same path)."""
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.parent.chmod(0o700)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n")
        p.chmod(0o600)
        return p

    @classmethod
    def load(cls, path: Path) -> RotationState:
        return cls(**json.loads(path.read_text()))


def mint_secret() -> str:
    return secrets.token_urlsafe(_MINTED_SECRET_BYTES)


def build_state(new_secret: str | None = None) -> RotationState:
    """Snapshot the current cluster's identity/ports/secret. Refuses when the
    db and redis URLs disagree on identity — that is pre-existing drift a
    rotation must not paper over."""
    db_identity = identity_from_url(settings.data_plane.db_url)
    redis_identity = identity_from_url(settings.data_plane.redis_url)
    if db_identity != redis_identity:
        raise RuntimeError(
            f"db_url identity {db_identity!r} != redis_url identity {redis_identity!r} — "
            "this cluster's data-plane identity is already inconsistent; fix that "
            "before rotating its secret."
        )
    # The direct pg port and the pooler port are REGISTRY facts (the one-URL
    # design: AVA_DB_URL carries the pooler port when pooling is on, so reading
    # pg_port off the URL would hand the rotation the pooler's port). Derive both
    # from the registry record; a missing record is refused rather than guessed
    # (rotation mutates the real data plane).
    from shared.cluster import get_record, record_pgbouncer_port, record_postgres_port
    from shared.paths import ava_home

    rec = get_record(ava_home())
    if rec is None:
        raise RuntimeError(
            "no registry record for this home — cannot resolve the data-plane ports; "
            "is this cluster installed (scripts/install.sh)?"
        )
    pg_port = record_postgres_port(rec)
    redis_port = urlsplit(settings.data_plane.redis_url).port
    if not redis_port:
        raise RuntimeError(
            "redis_url carries no port — is this cluster's data plane provisioned (ava start)?"
        )
    return RotationState(
        identity=db_identity,
        old_secret=settings.data_plane.cluster_secret,
        new_secret=new_secret or mint_secret(),
        pg_port=pg_port,
        redis_port=redis_port,
        pgbouncer_enabled=settings.data_plane.pgbouncer_enabled,
        pgbouncer_port=record_pgbouncer_port(rec),
    )


# ─────────────────────────── read-only probes ───────────────────────────


def _pg_probe(identity: str, pg_port: int, secret: str) -> bool:
    """True iff `identity` authenticates to its own database over TCP loopback
    with `secret`. Deliberately dials TCP (not the trust socket) — the whole
    point is proving the password, not just reachability."""
    import psycopg

    url = f"postgresql://{identity}:{secret}@127.0.0.1:{pg_port}/{identity}"
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("select 1")
        return True
    except Exception:
        return False


def _redis_probe(redis_port: int, password: str, *, username: str | None = None) -> bool:
    """PING as `username` (`default` when omitted — the admin/requirepass user
    `ensure_cluster_redis_acl`'s own admin dial and `ava status` both use) or as
    the cluster's ACL identity user, the credential the real `AVA_REDIS_URL`
    carries and what agents/gateway actually authenticate as."""
    import redis

    try:
        with redis.Redis(
            host="127.0.0.1",
            port=redis_port,
            username=username,
            password=password,
            socket_connect_timeout=3,
        ) as r:
            return bool(r.ping())
    except Exception:
        return False


def _pgbouncer_probe(state: RotationState, secret: str) -> bool:
    from cli.commands._pgbouncer import pgbouncer_reachable

    return pgbouncer_reachable(state.pgbouncer_port, state.identity, state.identity, secret)


def preflight(state: RotationState) -> bool:
    """Read-only: confirm the CURRENT secret already authenticates everywhere.
    Rotating on top of pre-existing drift would compound it, not fix it."""
    print("\npreflight (read-only):")
    ok_pg = _pg_probe(state.identity, state.pg_port, state.old_secret)
    print(f"  {'✓' if ok_pg else '✗'} postgres :{state.pg_port} (role {state.identity!r})")
    ok_redis_admin = _redis_probe(state.redis_port, state.old_secret)
    print(f"  {'✓' if ok_redis_admin else '✗'} redis :{state.redis_port} (default/requirepass)")
    ok_redis_identity = _redis_probe(state.redis_port, state.old_secret, username=state.identity)
    print(
        f"  {'✓' if ok_redis_identity else '✗'} redis :{state.redis_port} (ACL user {state.identity!r})"
    )
    ok_pgbouncer = True
    if state.pgbouncer_enabled and state.pgbouncer_port:
        ok_pgbouncer = _pgbouncer_probe(state, state.old_secret)
        print(f"  {'✓' if ok_pgbouncer else '✗'} pgbouncer :{state.pgbouncer_port}")
    ok = ok_pg and ok_redis_admin and ok_redis_identity and ok_pgbouncer
    if not ok:
        print(
            "  ✗ the CURRENT secret does not already authenticate everywhere — this is "
            "pre-existing drift, not something rotation fixes. Resolve it first "
            "(see conventions/runbook.md).",
            file=sys.stderr,
        )
    return ok


def _agent_runner_roster() -> list[tuple[str, str | None]]:
    """Best-effort read of the `machines` table's agent-runner roster (name,
    url) — never raises; a DB hiccup here must not block a dry-run report."""
    try:
        from shared.machines import list_agent_runners

        return list_agent_runners()
    except Exception as exc:
        print(f"  (could not read the machines roster: {exc})", file=sys.stderr)
        return []


def print_plan(state: RotationState, *, dry_run: bool) -> None:
    print(f"identity:          {state.identity!r}")
    print(f"postgres port:     {state.pg_port}")
    print(f"redis port:        {state.redis_port}")
    print(
        f"pgbouncer:         {'enabled, port ' + str(state.pgbouncer_port) if state.pgbouncer_enabled else 'disabled'}"
    )
    print(f"mode:              {'DRY RUN (read-only)' if dry_run else 'EXECUTE (will mutate)'}")
    roster = _agent_runner_roster()
    print(f"\nenrolled agent-runners ({len(roster)}) — push AVA_CLUSTER_SECRET to each")
    print("out-of-band AFTER this gateway is rotated, then restart it (see runbook):")
    for name, url in roster:
        print(f"  - {name} ({url or 'no url on record'})")
    if not roster:
        print("  (none on record — single-box cluster, or the roster is empty)")


# ─────────────────────────── mutating phases ───────────────────────────


def _working_redis_admin_password(state: RotationState) -> str:
    """The `default` user's CURRENT password: the old secret unless a prior,
    interrupted attempt already flipped it to the new one (resume case)."""
    if _redis_probe(state.redis_port, state.old_secret):
        return state.old_secret
    if _redis_probe(state.redis_port, state.new_secret):
        return state.new_secret
    raise RuntimeError(
        f"redis :{state.redis_port} authenticates with neither the old nor the new "
        "secret as `default` — requirepass is in an unknown state; recover manually."
    )


def apply_pg_role(state: RotationState) -> None:
    """Re-affirm the Postgres role's password to the new secret. Connects over
    the loopback trust socket (passwordless provisioning), so this never
    depends on which secret is currently live."""
    ensure_cluster_role(
        state.identity,
        base_admin_url=pg_admin_url(state.pg_port),
        cluster_secret=state.new_secret,
    )


def apply_redis_acl(state: RotationState) -> None:
    admin_password = _working_redis_admin_password(state)
    ensure_cluster_redis_acl(
        state.identity,
        redis_admin_url=f"redis://default:{admin_password}@127.0.0.1:{state.redis_port}",
        cluster_secret=state.new_secret,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
    )


def apply_redis_requirepass(state: RotationState) -> None:
    import redis

    admin_password = _working_redis_admin_password(state)
    with redis.Redis(
        host="127.0.0.1", port=state.redis_port, password=admin_password, socket_connect_timeout=3
    ) as r:
        r.execute_command("CONFIG", "SET", "requirepass", state.new_secret)  # pyright: ignore[reportUnknownMemberType]


def apply_pgbouncer(state: RotationState) -> None:
    if not (state.pgbouncer_enabled and state.pgbouncer_port):
        print("  (pgbouncer disabled/unconfigured — skipped)")
        return
    from cli.commands._pgbouncer import ensure_pgbouncer, runner_password_from_env

    rc = ensure_pgbouncer(
        pg_port=state.pg_port,
        listen_port=state.pgbouncer_port,
        db_name=state.identity,
        role=state.identity,
        cluster_secret=state.new_secret,
        # The rewritten userlist must keep the ava_runner entry (Task #1236) —
        # a rotation that dropped it would break every runner at its next dial.
        runner_password=runner_password_from_env(),
    )
    if rc != 0:
        raise RuntimeError("ensure_pgbouncer failed — see its own stderr output above")


def verify(state: RotationState) -> None:
    """Prove the rotation actually took effect: new secret works, old secret no
    longer does (a silent no-op would be worse than a loud failure)."""
    if not _pg_probe(state.identity, state.pg_port, state.new_secret):
        raise RuntimeError("postgres does not yet authenticate with the NEW secret")
    if state.old_secret != state.new_secret and _pg_probe(
        state.identity, state.pg_port, state.old_secret
    ):
        raise RuntimeError("postgres STILL authenticates with the OLD secret — rotation no-opped")
    if not _redis_probe(state.redis_port, state.new_secret):
        raise RuntimeError("redis (default) does not yet authenticate with the NEW secret")
    if state.old_secret != state.new_secret and _redis_probe(state.redis_port, state.old_secret):
        raise RuntimeError(
            "redis (default) STILL authenticates with the OLD secret — rotation no-opped"
        )
    if not _redis_probe(state.redis_port, state.new_secret, username=state.identity):
        raise RuntimeError(
            f"redis ACL user {state.identity!r} does not yet authenticate with the NEW secret"
        )
    if state.old_secret != state.new_secret and _redis_probe(
        state.redis_port, state.old_secret, username=state.identity
    ):
        raise RuntimeError(
            f"redis ACL user {state.identity!r} STILL authenticates with the OLD secret — "
            "rotation no-opped"
        )
    if (
        state.pgbouncer_enabled
        and state.pgbouncer_port
        and not _pgbouncer_probe(state, state.new_secret)
    ):
        raise RuntimeError("pgbouncer does not yet accept the NEW secret")


def write_env(state: RotationState) -> None:
    """Rewrite this gateway's own `.env`: the secret itself, plus the password
    embedded in `AVA_DB_URL`/`AVA_REDIS_URL` — `url_with_password` keeps every
    other URL part (host/port/db/username) untouched, mirroring exactly what
    `DataPlaneSettings._apply_cluster_secret` re-derives on every Settings load.
    `upsert_env` snapshots the pre-rotation `.env` first."""
    upsert_env(
        AVA_ENV_PATH,
        {
            "AVA_CLUSTER_SECRET": state.new_secret,
            "AVA_DB_URL": url_with_password(settings.data_plane.db_url, state.new_secret),
            "AVA_REDIS_URL": url_with_password(settings.data_plane.redis_url, state.new_secret),
        },
    )


def run_phase(state: RotationState, phase: str, fn: Callable[[RotationState], None]) -> None:
    print(f"-> {phase}")
    fn(state)
    state.phase = phase
    state.save()
    print(f"   ✓ {phase}")


# ─────────────────────────── CLI ───────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate this cluster's AVA_CLUSTER_SECRET.")
    parser.add_argument(
        "--execute", action="store_true", help="perform the rotation (default: dry-run, read-only)"
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the interactive confirmation prompt for --execute"
    )
    parser.add_argument(
        "--resume",
        metavar="STATE_FILE",
        default=None,
        help="continue an interrupted rotation from its recovery state file "
        "(same target secret; every phase is safe to re-run)",
    )
    args = parser.parse_args(argv)
    dry_run = not args.execute

    state = RotationState.load(Path(args.resume)) if args.resume else build_state()
    print_plan(state, dry_run=dry_run)

    if dry_run:
        preflight(state)
        print("\n[dry-run] no changes made. Re-run with --execute to perform the rotation.")
        return 0

    # preflight's "the OLD secret already authenticates everywhere" premise is
    # true by construction for a FRESH rotation, and false by construction for
    # a --resume of one already interrupted mid-phase — so it only gates a
    # fresh start. A resumed run's own phases (all idempotent) surface a clear
    # error if something is genuinely broken.
    if not args.resume and not preflight(state):
        print("\n✗ refusing to --execute on top of pre-existing drift.", file=sys.stderr)
        return 1

    if not args.yes:
        answer = input(
            f"\nAbout to rotate the cluster secret for identity={state.identity!r} "
            f"(pg :{state.pg_port}, redis :{state.redis_port}). Type 'rotate' to continue: "
        )
        if answer.strip() != "rotate":
            print("aborted.")
            return 1

    try:
        run_phase(state, "pg_role", apply_pg_role)
        run_phase(state, "redis_acl", apply_redis_acl)
        run_phase(state, "redis_requirepass", apply_redis_requirepass)
        run_phase(state, "pgbouncer", apply_pgbouncer)
        run_phase(state, "verified", verify)
        run_phase(state, "env_written", write_env)
    except Exception as exc:
        state_path = state.save()
        print(f"\n✗ ROTATION FAILED at phase {state.phase!r}: {exc}", file=sys.stderr)
        print(
            f"  recovery state (0600, holds both secrets in plaintext): {state_path}\n"
            f"  every phase is idempotent — resume with:\n"
            f"    .venv/bin/python scripts/rotate_cluster_secret.py --execute --resume {state_path}",
            file=sys.stderr,
        )
        return 1

    state.phase = "complete"
    state_path = state.save()
    print(f"\n✓ rotation complete. Recovery state kept at {state_path} — delete it once every")
    print("  enrolled agent-runner below has been pushed the new secret and confirmed healthy:")
    print_plan(state, dry_run=False)
    print(
        "\nNEXT (not done by this script): bounce this gateway process so its own Settings "
        "load the new .env, then push+restart each agent-runner above."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
