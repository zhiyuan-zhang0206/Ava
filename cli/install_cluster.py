"""Cluster birth at install time — `python -m cli.install_cluster`.

The thin entry `scripts/install.sh` runs as its final step, so an install ends
with a born cluster: registry record (keyed by the home path — identity IS the
path, there is no cluster name) + the cluster's own Postgres/Redis instance +
provisioned database + `$AVA_HOME/.env` (the cluster secret follows the role —
see below; the db/role/ACL identifier is the fixed
`shared.cluster.DATA_PLANE_IDENTITY`). `ava start` then finds the record and is
a pure bring-up. A role without `gateway` does not birth — an agent-runner's
connection facts arrive via `ava enroll` — it only gets the serve flags
written. Nothing is ever started here; that stays `ava start`.

The `--role` capability set also decides the serve flags
(AVA_MACHINE_SERVE_GATEWAY / AVA_MACHINE_SERVE_AGENT_RUNNER), written into the
home's `.env` so a later `ava start` resolves them with no `--serve-*` flags.

The cluster secret follows the role (user decision: off is fully off). A
single-machine role (`gateway,agent-runner` on one box) births a NO-AUTH
cluster: the secret stays empty unless `AVA_INSTALL_CLUSTER_SECRET` states one, and every
surface (gateway API, /ops, pg/redis) serves unauthenticated on loopback. A
split `gateway`-only host mints a fresh secret (remote agent-runners depend on
it — scram/requirepass + bearer auth). The compatibility `--cluster-secret`
flag overrides the dedicated environment input. A secret already in the home's
`.env` is never rotated.

`--worktree` is the `install.sh --worktree` backend: births a dev worktree's
own cluster under `--home` (install.sh derives the default
`~/.ava-<checkout-dir>` from the checkout location, never the cwd; any path
works — the path needs no naming convention), writes the checkout's `.ava_home`
pointer, and — with `--seed --seed-source <env>` — copies the
`shared.env_registry.seed_allowlist()` allowlist (LLM + web-search keys, plus
the DashScope base URL those keys are minted against) from the stated source.
The cluster secret is never seeded or inherited: a worktree
birth is single-machine, so it is a NO-AUTH cluster (empty secret) unless
`AVA_INSTALL_CLUSTER_SECRET` states one.

Settings-load bootstrap: this process may target a home with no `.env` yet,
while Settings requires AVA_DB_URL / AVA_REDIS_URL. `main()` therefore pins
AVA_HOME, strips every inherited cluster/identity env key (a shell that sourced
the prod env must not leak it into the new cluster), and plants never-dialed
placeholder URLs — all BEFORE the first settings-touching import. Every real
connection in this module goes through explicit admin/base URLs
(`pg_admin_url` / `derive_env` args), never `settings.data_plane`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_VALID_CAPS = frozenset({"gateway", "agent-runner"})
_CLUSTER_BIRTH_SECRET_KEY = "AVA_INSTALL_CLUSTER_SECRET"  # noqa: S105 — key, not credential


def _install_secret_input(explicit: str | None) -> str | None:
    """Consume the dedicated one-shot install secret.

    The compatibility argv flag wins when both are present. The generic
    AVA_CLUSTER_SECRET is intentionally never read: a shell carrying a live
    cluster's runtime environment must not seed a different cluster.
    """
    from_env = os.environ.pop(_CLUSTER_BIRTH_SECRET_KEY, None)
    return explicit if explicit is not None else from_env


def _checkout_root() -> Path:
    """The checkout this module runs from (`cli/` -> repo root). Anchored to
    `__file__`, never the cwd — a worktree shell's cwd can be reset elsewhere."""
    return Path(__file__).resolve().parents[1]


def _bootstrap_process_env(home: Path) -> None:
    """Prepare the process environment BEFORE any settings-touching import.

    - Pins AVA_HOME to the install target so `shared.dotenv_boot` resolves (and
      loads) THAT home's `.env` — structurally never the prod one.
    - Strips every inherited cluster/identity key (DERIVED + IDENTITY sets): a
      shell that sourced the prod env, or a parent that loaded it, must not leak
      the prod data plane / secret / serve flags into the new cluster.
    - Plants never-dialed placeholder db/redis URLs so Settings (which requires
      them) constructs on a home with no `.env` yet. When the home's `.env`
      exists, `dotenv_boot` forces the derived keys from the file, so the real
      values win over these placeholders.
    - Drops AVA_HOME_OVERRIDE once the exemption is consumed (the import of
      dotenv_boot above ran resolve_ava_home, the only reader), so the hatch
      cannot leak into children this process spawns.
    """
    os.environ["AVA_HOME"] = str(home)
    # The install DECIDES which home this checkout owns — it writes the
    # `.ava_home` pointer that `resolve_ava_home` later reads. So the pin above
    # outranking an existing pointer is correct here and nowhere else: without
    # this, re-pointing a checkout at a new home (`--path` naming a different
    # cluster than the last install) would abort as a contradiction against the
    # pointer this very run is about to replace.
    os.environ["AVA_HOME_OVERRIDE"] = "1"
    # Install-time Settings constructions must not fetch from a gateway: a fresh
    # home has no gateway URL yet (an agent-runner-only install enrolls later),
    # and the install decides the unit's shape itself. The fetch skip applies to
    # this process only — daemons the install later starts fetch per their role
    # (shared.session_env does not forward AVA_CONFIG_FETCH).
    from shared.bootstrap import CONFIG_FETCH_ENV, CONFIG_FETCH_SKIP

    os.environ[CONFIG_FETCH_ENV] = CONFIG_FETCH_SKIP
    from shared.env_registry import derived_env_keys, env_identity_keys

    for key in derived_env_keys() | env_identity_keys():
        os.environ.pop(key, None)
    # The redis placeholder lives in dotenv_boot beside the DB sentinel so the
    # authority pass exempts both symmetrically (F-s4-6) — a copy here would
    # silently fall out of the drop loop's exemption set. Imported HERE, not at
    # module top: importing dotenv_boot triggers its eager resolve_ava_home(),
    # which must run only after this function pinned AVA_HOME + the override.
    from shared.dotenv_boot import BOOT_REDIS_PLACEHOLDER, UNANCHORED_DB_SENTINEL

    os.environ.setdefault("AVA_DB_URL", UNANCHORED_DB_SENTINEL)
    os.environ.setdefault("AVA_REDIS_URL", BOOT_REDIS_PLACEHOLDER)
    # The exemption above has now been CONSUMED: importing dotenv_boot ran its
    # eager resolve_ava_home(), the one place AVA_HOME_OVERRIDE is read. Strip
    # it so no child this process spawns (pgbouncer / pg_ctl / psql today, any
    # future Python subprocess) inherits the contradiction hatch — the
    # "cannot become ambient" boundary the decision doc relies on (F-s4-7).
    os.environ.pop("AVA_HOME_OVERRIDE", None)


def _serve_flag_env(role: frozenset[str]) -> dict[str, str]:
    """The serve flags `--role` decides, written into the home's `.env` so a
    later `ava start` resolves the capabilities with no `--serve-*` flags
    (resolution is env > file > arg, and `.env` loads into the env)."""
    return {
        "AVA_MACHINE_SERVE_GATEWAY": "true" if "gateway" in role else "false",
        "AVA_MACHINE_SERVE_AGENT_RUNNER": "true" if "agent-runner" in role else "false",
    }


def _resolve_secret(env_path: Path, *, role: frozenset[str], explicit: str | None) -> str:
    """The cluster secret, in precedence order:

    1. `explicit` (preferred `AVA_INSTALL_CLUSTER_SECRET`, or the compatibility
       `--cluster-secret`) — the operator states the secret; it is
       validated URL-safe (it lands in the pg/redis URLs, redis.conf, and HTTP
       bearer headers).
    2. The one already in this home's `.env` (a re-install must not rotate a
       live cluster's data-plane password).
    3. A single-machine role (`gateway,agent-runner` on one box) leaves the
       secret EMPTY — a no-auth cluster by default (user decision: off is fully
       off; every surface serves unauthenticated on loopback).
    4. Any other gateway-capable role (a split `gateway`-only host) mints a
       fresh URL-safe token — remote agent-runners depend on the secret
       (scram/requirepass + bearer), so a split deployment always has one.

    NEVER read from the process environment — an inherited prod
    AVA_CLUSTER_SECRET must not become this cluster's secret."""
    from dotenv import dotenv_values

    if explicit is not None:
        if not _is_urlsafe_token(explicit):
            raise ValueError(
                "the install cluster secret must be a URL-safe token (letters, digits, and "
                "'._~-') — it goes in the pg/redis URLs, the redis config, and a "
                "bearer header"
            )
        return explicit
    if env_path.exists():
        existing = (dotenv_values(env_path).get("AVA_CLUSTER_SECRET") or "").strip()
        if existing:
            return existing
    if role == frozenset({"gateway", "agent-runner"}):
        # Single box: no auth by default (see the module docstring).
        return ""
    import secrets

    return secrets.token_urlsafe(32)


def _is_urlsafe_token(value: str) -> bool:
    """URL-safe charset check mirroring DataPlaneSettings.cluster_secret — the
    value lands in URLs, redis.conf, and bearer headers, so the same restriction
    applies at install time (install_cluster cannot build Settings yet)."""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
    return bool(value) and set(value) <= allowed


def _resolve_runner_password(env_path: Path) -> str:
    """The ava_runner role password: the value already in this home's `.env`
    (a re-install must not rotate a live cluster's runner credential), else a
    freshly minted URL-safe token. NEVER read from the process environment —
    an inherited prod AVA_RUNNER_DB_PASSWORD must not become this cluster's
    runner password (same rule as `_resolve_secret`).
    """
    from dotenv import dotenv_values

    if env_path.exists():
        existing = (dotenv_values(env_path).get("AVA_RUNNER_DB_PASSWORD") or "").strip()
        if existing:
            return existing
    import secrets

    return secrets.token_urlsafe(32)


def seed_convenience_env(*, target_env: Path, source_env: Path) -> list[str]:
    """Copy the seed_allowlist() allowlist (LLM provider + web search/fetch keys,
    plus the DashScope base URL those keys are minted against) from `source_env`
    (the prod home's `.env`) into `target_env`. Convenience
    only — identity/derived keys, the cluster secret, and singleton credentials
    are structurally outside the allowlist. Returns the copied key names;
    missing source is a no-op (a box with no prod install seeds nothing)."""
    from dotenv import dotenv_values

    from shared.env_registry import seed_allowlist
    from shared.envfile import upsert_env

    if not source_env.exists():
        print(f"  · seed: no {source_env} to copy convenience keys from (skipped)")
        return []
    picked = {k: v for k, v in dotenv_values(source_env).items() if k in seed_allowlist() and v}
    if picked:
        upsert_env(target_env, picked)
    print(f"  · seeded {len(picked)} convenience key(s) from {source_env}")
    return sorted(picked)


def _rollback_record(created: bool, home: Path) -> None:  # noqa: FBT001 — rollback gate, passed by name
    """Deregister a record THIS install allocated when a later step fails, so a
    crashed birth leaks no orphan port block."""
    if not created:
        return
    from shared import cluster as cl

    cl.delete_record_locked(home)  # lock already held — see _rollback_record
    print(f"  · rolled back registry entry for '{home}' (port block freed)", file=sys.stderr)


def _birth(*, home: Path, secret: str) -> int:
    """Registry record (keyed by home) + this cluster's own pg/redis instance +
    provisioned database + derived `.env`. Never starts anything.

    The data-plane identifier (db / role / ACL user) is the fixed
    `DATA_PLANE_IDENTITY` — birth is the one place the identifier is *chosen*;
    everywhere else reads it back from the `.env` URLs as data."""
    from cli.commands import cluster_lifecycle as lifecycle
    from shared import cluster as cl
    from shared.envfile import upsert_env

    rec, created = lifecycle._ensure_record(home)
    home.mkdir(parents=True, exist_ok=True)

    runner_password = _resolve_runner_password(home / ".env")
    rc = lifecycle._ensure_cluster_instance(
        rec, secret, cl.DATA_PLANE_IDENTITY, runner_password=runner_password
    )
    if rc != 0:
        _rollback_record(created, home)
        print(
            f"✗ install_cluster: data plane bring-up failed for '{home}' (rc={rc})", file=sys.stderr
        )
        return rc
    try:
        from cli.commands._cluster_instance import pg_admin_url

        base_admin_url = pg_admin_url(rec.ports["postgres"])
        base_db_url, base_redis_url = cl.per_cluster_base_urls(rec)
        database_created = lifecycle._provision(
            cl.DATA_PLANE_IDENTITY, base_admin_url=base_admin_url, cluster_secret=secret
        )
        # The runner's least-privilege surface: checkpoint tables first (created
        # as the main role, so the gateway's own checkpoint readers keep working),
        # then the ava_runner role + grants — a fresh birth has no pre-existing
        # checkpoint tables, so the grants must come after the schema ensure.
        cl.ensure_checkpoint_schema(
            cl.DATA_PLANE_IDENTITY,
            base_admin_url=base_admin_url,
            cluster_secret=secret,
            database_created=database_created,
            resume_partial=True,
        )
        cl.ensure_runner_role(
            cl.DATA_PLANE_IDENTITY,
            base_admin_url=base_admin_url,
            runner_password=runner_password,
        )
        # The one DB URL's port is decided at generation by the pooler toggle
        # (default on): AVA_PGBOUNCER_ENABLED already in this home's .env (a
        # re-install) wins; a fresh birth defaults to pooling. Converge re-normalizes
        # on every start, so a later toggle flip self-heals the URL.
        from dotenv import dotenv_values

        enabled = (dotenv_values(home / ".env").get("AVA_PGBOUNCER_ENABLED") or "true").strip()
        pgbouncer_enabled = enabled.lower() in ("1", "true", "yes", "on")
        env_vars = cl.derive_env(
            rec,
            base_db_url=base_db_url,
            base_redis_url=base_redis_url,
            cluster_secret=secret,
            pgbouncer_enabled=pgbouncer_enabled,
        )
        # The runner role password is a gateway-side secret (never a derived
        # cluster fact, never a bootstrap field): persisted beside the derived
        # env so the pgbouncer userlist and the bootstrap projection read it
        # from the file, and a re-install keeps the existing value.
        env_vars["AVA_RUNNER_DB_PASSWORD"] = runner_password
        upsert_env(home / ".env", env_vars)
    except Exception:
        _rollback_record(created, home)
        raise
    print(f"→ cluster born at {home} (gateway port={rec.ports['gateway']})")
    return 0


def cmd_install_cluster(
    *,
    home: Path,
    role: frozenset[str],
    worktree: bool,
    seed: bool,
    seed_source: Path | None = None,
    cluster_secret: str | None = None,
) -> int:
    """Install-time birth (idempotent). See the module docstring for the flow.

    Identity is the path — the home is the only identity input; there is no
    name to derive or validate. Returns 0 on success; 1 on a refused target.
    """
    from shared import cluster as cl
    from shared.envfile import upsert_env

    env_path = home / ".env"

    if "gateway" not in role:
        # No local cluster to birth — connection facts arrive via `ava enroll`.
        # Only the serve flags are persisted so `ava start` needs no capability
        # flags.
        upsert_env(env_path, _serve_flag_env(role))
        print(
            f"  · wrote serve flags to {env_path} (role={','.join(sorted(role))}; "
            "no birth — `ava enroll` supplies the cluster)"
        )
        return 0

    if worktree and cl.is_default_home(home):
        print(
            f"✗ install_cluster: refusing --worktree birth at the default home "
            f"({cl.default_home()}) — that is the prod cluster. Pass --path <other-home>.",
            file=sys.stderr,
        )
        return 1

    existing = cl.get_record(home)
    if worktree and existing is not None and not _pointer_matches(home):
        # The home is registered but this checkout never birthed it (no matching
        # `.ava_home` pointer) — another checkout owns that home (e.g. two
        # worktrees sharing a directory name and thus the default home). Silently
        # reusing it would clobber the other worktree's cluster.
        print(
            f"✗ install_cluster: home '{home}' is already registered to another "
            "checkout (this one carries no matching .ava_home pointer). Re-run "
            f"with an explicit --path, or reclaim it: ava cluster destroy --path {home}.",
            file=sys.stderr,
        )
        return 1

    from dotenv import dotenv_values

    installed = (
        existing is not None
        and env_path.exists()
        and bool(dotenv_values(env_path).get("AVA_DB_URL"))
    )
    if installed:
        print(f"✓ cluster already installed at {home} (birth is a no-op)")
    else:
        rc = _birth(home=home, secret=_resolve_secret(env_path, role=role, explicit=cluster_secret))
        if rc != 0:
            return rc

    upsert_env(env_path, _serve_flag_env(role))
    # Turnkey start: a worktree cluster is axiomatically a single-box
    # gateway,agent-runner install, so pre-write the machine identity too —
    # `ava start` then needs ZERO flags (serve flags + AVA_GATEWAY_URL are
    # already in .env; machine_name was the last missing required field).
    # Default = the home basename (dots stripped) — unique per home, lives
    # only in this cluster's own db. Never done for a prod install, which
    # must declare its public identity explicitly.
    if worktree and not (dotenv_values(env_path).get("AVA_MACHINE_NAME") or "").strip():
        machine = home.name.lstrip(".") or "ava"
        upsert_env(env_path, {"AVA_MACHINE_NAME": machine})
        print(f"  · wrote AVA_MACHINE_NAME={machine} (worktree default; edit .env to rename)")
    if not cl.is_default_home(home):
        # Checkout-anchored home pointer: bare invocations from this checkout
        # resolve to this cluster's home instead of the prod ~/.ava. The prod
        # source needs no pointer (auto-detected by resolve_ava_home).
        pointer = _checkout_root() / ".ava_home"
        pointer.write_text(f"{home}\n")
        print(f"  · wrote {pointer} -> {home}")
    if seed:
        seed_convenience_env(
            target_env=env_path, source_env=seed_source or Path.home() / ".ava" / ".env"
        )
    return 0


def _pointer_matches(home: Path) -> bool:
    """True when this checkout's `.ava_home` pointer exists and names `home` —
    the proof that THIS checkout birthed the cluster living there."""
    pointer = _checkout_root() / ".ava_home"
    return pointer.exists() and pointer.read_text().strip() == str(home)


def cmd_seed_only(*, home: Path, seed_source: Path | None = None) -> int:
    """Re-run just the convenience-key seed against an already-installed home."""
    env_path = home / ".env"
    if not env_path.exists():
        print(
            f"✗ install_cluster: {env_path} does not exist — install the cluster "
            "first (scripts/install.sh), then re-seed.",
            file=sys.stderr,
        )
        return 1
    seed_convenience_env(
        target_env=env_path, source_env=seed_source or Path.home() / ".ava" / ".env"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m cli.install_cluster",
        description="install-time cluster birth (invoked by scripts/install.sh; "
        "idempotent, never starts services)",
        # No abbreviated options: `--cluster` must not silently become
        # `--cluster-secret` — an install typo is a silent misconfiguration.
        allow_abbrev=False,
    )
    p.add_argument(
        "--home", required=True, help="the cluster home ($AVA_HOME) this install targets"
    )
    p.add_argument(
        "--role",
        required=True,
        help="capability set (comma-separated: gateway,agent-runner); decides the "
        "serve flags and whether a cluster is birthed (gateway-capable only). "
        "No default — the install must state the unit's shape explicitly.",
    )
    p.add_argument(
        "--cluster-secret",
        default=None,
        help="compatibility input for the cluster secret; prefer the one-shot "
        "AVA_INSTALL_CLUSTER_SECRET environment variable so it stays out of argv. "
        "Omitted: a single-machine role (gateway,agent-runner) births a NO-AUTH "
        "cluster (empty secret — every surface serves unauthenticated on "
        "loopback); a gateway-only split host mints a fresh one (remote "
        "agent-runners depend on it). An explicit input overrides a secret already "
        "in the home's .env; otherwise a re-install never rotates it.",
    )
    p.add_argument(
        "--worktree",
        action="store_true",
        help="worktree mode: refuse the default home, refuse a home another "
        "checkout owns, write this checkout's .ava_home pointer",
    )
    p.add_argument(
        "--seed",
        action="store_true",
        help="after birth, copy the SEED_ENV_KEYS allowlist from --seed-source",
    )
    p.add_argument(
        "--seed-only",
        action="store_true",
        help="only run the convenience-key seed against an installed home (no birth)",
    )
    p.add_argument(
        "--seed-source",
        default=None,
        help="the .env to seed convenience keys from (default: ~/.ava/.env — "
        "the same fallback the seed functions themselves use). Only read "
        "when --seed / --seed-only is passed.",
    )
    args = p.parse_args(argv)

    home = Path(args.home).expanduser()
    cluster_secret = _install_secret_input(args.cluster_secret)
    _bootstrap_process_env(home)
    seed_source = Path(args.seed_source).expanduser() if args.seed_source else None

    if args.seed_only:
        return cmd_seed_only(home=home, seed_source=seed_source)

    role = frozenset(tok.strip() for tok in args.role.split(",") if tok.strip())
    if not role or not role <= _VALID_CAPS:
        p.error(f"invalid --role {args.role!r} (tokens: gateway, agent-runner)")

    return cmd_install_cluster(
        home=home,
        role=role,
        worktree=args.worktree,
        seed=args.seed,
        seed_source=seed_source,
        cluster_secret=cluster_secret,
    )


if __name__ == "__main__":
    sys.exit(main())
