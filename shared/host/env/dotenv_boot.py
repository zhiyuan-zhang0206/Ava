"""Single .env load entry — every process entry point imports this.

The `.env` source lives under this unit's home: `$AVA_HOME/.env`. Co-located
gateway and runner units each carry their own home (`~/.ava_gateway` vs
`~/.ava`) and therefore their own `.env`; the dev clone and prod path on a
single-home machine share one `.env`. No `.env` lives in the repo root.

Home resolution is **checkout-anchored**: which checkout this code lives in is
the prod/dev discriminator (prod code under `~/.ava/source`, dev code under a
worktree path), so a bare invocation knows its home without the caller passing
anything. Precedence (see `resolve_ava_home`):

    1. AVA_HOME env var          - explicit; gateway-launched + prod sessions set it
    2. checkout == ~/.ava/source - the prod source -> ~/.ava
    3. <checkout>/.ava_home      - a dev cluster's home pointer (`ava start`)
    4. else                      - UNANCHORED: a private scratch home, never ~/.ava

Rule 1 beating rules 2-3 is only safe while they agree. When they disagree the
process has two different clusters' facts in hand — the env var's `.env`
(database URL, secret, ports) and the checkout's code (`migrations/`, skills,
plugin images) — and there is no defensible way to pick one, because whichever
side wins, the other half of the process's world comes from the loser. So a
contradiction raises `AvaHomeContradictionError` instead of resolving (see
`resolve_ava_home`); `AVA_HOME_OVERRIDE` opts out for callers that mean it.

Case 4 is a checkout that claims no cluster: a dev worktree that never ran
`ava start --worktree`, a fresh clone, a CI checkout. It owns no home, and the
default home `~/.ava` belongs to the prod source alone — resolving there made an
ad-hoc `import shared.config` on a production agent-runner load that unit's
`.env`, dial its gateway with its cluster bearer and rewrite its bootstrap
snapshot (2026-09-27). So case 4 resolves to a per-process scratch path under
the system temp dir (`_unanchored_home`; not created there, but
`shared.paths.ava_home()` creates it on first access — including a read-only
command, not only one that writes — so most CLI verbs leave it behind) and
boots BARE, exactly like a CI checkout: no `.env` / `mirror.env` is
read, the config source decision never fetches and the bootstrap transport
refuses to dial (shared/config/_lite.py, shared/host/env/bootstrap.py), and
`load_ava_env` plants `UNANCHORED_DB_SENTINEL` as AVA_DB_URL (the same sentinel
tests/conftest.py plants for unprovisioned test runs) so a DB connection fails
loudly (shared/db.connect raises an actionable error). Lint scripts, codegen
hooks and dev tools keep working from any checkout; every verb that would act
on "this checkout's cluster" refuses (`checkout_anchored()`, cli/preflight.py).

Callers:
    shared/config.py            - before importing Settings
    scripts/start_agent.py      - bootstrap root agent

`load_dotenv` itself is idempotent + does not overwrite already-set
os.environ keys; repeated calls have no side effects.
"""

from __future__ import annotations

import functools
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv

# Planted as AVA_DB_URL when home resolution lands on an unanchored dev
# checkout. A syntactically valid URL that can never reach a real database (port
# 1 on loopback), so a stray connection fails loudly instead of silently hitting
# the prod database the host .env points at. shared/db.connect() detects it and
# raises an actionable error directing the operator to `.venv/bin/ava start --worktree`.
UNANCHORED_DB_SENTINEL = "postgresql://unanchored-dev-checkout@127.0.0.1:1/run-ava-start-first"

# The launcher's process profile, recorded by the CLI entry point before it
# clears the live marker (cli/main.py `_normalize_process_profile`). The CLI is
# deliberately settings-full — no profile — but a boot pass reached through it
# must still see which launcher context this process tree descends from; the
# launcher-projection exemptions in `_enforce_cluster_env_authority` read it
# live-or-recorded (`_launcher_context`). Never cleared: a nested CLI only
# overwrites it when it carries a fresh live marker of its own (#4334).
LAUNCHER_PROFILE_ENV_KEY = "AVA_LAUNCHER_PROFILE"

_HOME_POINTER = ".ava_home"

# See `_is_launcher_runner_projection`.
_RUNNER_LOGIN = re.compile(r"ava_runner|ava_g(?:0|[1-9][0-9]*)_runner")

# The non-secret launch-environment marker that makes a launcher-injected
# AVA_DB_URL authoritative (mirrors shared.cluster.authority.GENERATION_ENV),
# and the delivered machine API token (shared.cluster.authority.api).
_GENERATION_ENV = "AVA_DB_GENERATION"
_API_TOKEN_ENV = "AVA_API_TOKEN"  # noqa: S105 — env key name, not a credential

# Why this process holds no database authority, when a home with a write-
# generation ledger delivered none to it: `shared.db.connections._guard_db_url`
# turns a dial of the credential-free endpoint into a named refusal.
_db_authority_refusal: str | None = None
# Name prefix of a case-4 scratch home (`_unanchored_home`).
_UNANCHORED_PREFIX = "ava-unanchored-"

# Opt out of the AVA_HOME-vs-checkout contradiction check (`resolve_ava_home`).
# For callers that redirect a checkout to a home it does not own ON PURPOSE and
# accept running that checkout's code against it — the test suite's scratch home
# (tests/conftest.py). Only the real process environment can open it: the check
# runs at this module's import, before any `.env` is loaded, so no cluster can grant itself
# the exemption on disk.
_HOME_OVERRIDE = "AVA_HOME_OVERRIDE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# A finalizer receives this one-use launch ticket alongside its private proof.
# `load_ava_env` consumes it before reading `.env`, so a value planted in the
# file cannot turn an arbitrary model child into a finalizer.
_manifest_finalizer_boot_authorized = False


class AvaHomeContradictionError(RuntimeError):
    """AVA_HOME names one cluster's home while the executing checkout claims another.

    Raised at import of this module (it resolves the home into `_HOME` eagerly),
    so the process dies before `load_ava_env` can put the wrong cluster's
    credentials in `os.environ` and before anything opens a connection with them.
    """


def _prod_source() -> Path:
    """The prod source checkout path (`~/.ava/source`) — the one checkout allowed
    to resolve to the bare `~/.ava` home without a pointer or explicit AVA_HOME."""
    return (Path.home() / ".ava" / "source").resolve()


def _checkout_root() -> Path:
    """The repo checkout this code lives in (`shared/host/env/dotenv_boot.py` -> repo root).

    Anchored to `__file__`, not cwd, so resolution is identical no matter where a
    bare script is launched from (the danger is ad-hoc scripts / subagents whose
    cwd is arbitrary)."""
    return Path(__file__).resolve().parents[3]


def _checkout_claim() -> tuple[Path, str] | None:
    """The home this checkout claims plus *why* it claims it, or None if it claims none.

    The reason string is what makes a contradiction actionable — the two rules
    that produce a claim are a file the operator can read and a path rule they
    cannot, so an error naming only the paths would leave "says who?" unanswered.
    """
    root = _checkout_root()
    if root == _prod_source():
        return Path.home() / ".ava", f"{root} is the prod source checkout"
    pointer = root / _HOME_POINTER
    if pointer.exists():
        target = pointer.read_text().strip()
        if target:
            return Path(target).expanduser(), f"{pointer} points there"
    return None


@functools.cache
def _unanchored_home() -> Path:
    """The scratch home of an unanchored checkout (case 4) — one per process.

    A path under the system temp dir with an unguessable suffix, never under
    `~/.ava` and never a registered cluster, so nothing another unit wrote can
    be read from it. Not created here — this function only names the path. But
    `shared.paths.ava_home()` creates it via `ensure_private_dir` the first time
    anything resolves `$AVA_HOME`, which most CLI verbs do even on a read-only
    run, so a purely read-only command still leaves this directory behind. The
    boot pins it as AVA_HOME, and its name marks it (`_is_unanchored_scratch`),
    so a re-resolution in this process and every child that inherits it stay
    unanchored instead of reading an explicit claim.
    """
    return Path(tempfile.gettempdir()) / f"{_UNANCHORED_PREFIX}{secrets.token_hex(8)}"


def _is_unanchored_scratch(home: Path) -> bool:
    """Whether `home` is a case-4 scratch home (this process's or a parent's)."""
    return re.fullmatch(rf"{_UNANCHORED_PREFIX}[0-9a-f]{{16}}", home.name) is not None


def checkout_anchored_home() -> tuple[Path, bool]:
    """Resolve the home **this checkout owns**, ignoring the AVA_HOME env var.

    `resolve_ava_home` minus its rule-1 override, i.e. cases 2-4 only. The env
    var says which cluster a process was *launched by*; this says which cluster
    the code on disk *belongs to*. The two diverge exactly when a process
    launched by one cluster runs code from another checkout — every agent
    process inherits `AVA_HOME=~/.ava` from the prod session env, so a bare
    `ava start` inside a dev worktree resolves to the prod home while running
    the worktree's code. `shared.deploy.schema.migrations` compares this against the identity
    the DB carries and refuses to migrate on a mismatch.

    `anchored` is False for case 4 (a checkout that claims no cluster), whose
    home is the process scratch (`_unanchored_home`) rather than a claim of
    ownership — callers proving ownership must treat False as "cannot prove it".
    """
    claim = _checkout_claim()
    if claim is None:
        return _unanchored_home(), False
    return claim[0], True


def _assert_env_agrees_with_checkout(env_home: Path) -> None:
    """Refuse when AVA_HOME names a different home than the checkout claims.

    The 2026-07-31 prod wedge (#1059) in one line: a fleet agent anchored a
    worktree to its own dev cluster — writing the worktree's `.ava_home` correctly —
    then ran `cd <worktree> && .venv/bin/ava start` from a shell carrying the prod
    session env. AVA_HOME=~/.ava outranked the pointer written two seconds earlier,
    so the worktree's `migrations/` were applied to the central prod database.
    Every fleet agent inherits that env, so the whole phantom-cluster incident
    class the checkout-anchored boot was meant to end was live again.

    Refusing is the only honest resolution. Preferring the pointer would break
    every legitimate AVA_HOME (a gateway-launched daemon, an enrolled runner),
    and preferring the env var is what caused the outage; the two disagreeing
    means the caller does not know which cluster they are operating on, which
    the process cannot decide for them.

    Paths are compared resolved, so `~/.ava` / `$HOME/.ava` / a symlinked tmpdir
    are not spurious contradictions.
    """
    if os.environ.get(_HOME_OVERRIDE, "").strip().lower() in _TRUTHY:
        return
    claim = _checkout_claim()
    if claim is None:
        return
    claimed, reason = claim
    if claimed.resolve() == env_home.resolve():
        return
    raise AvaHomeContradictionError(
        f"AVA_HOME={env_home} contradicts this checkout's own cluster {claimed} ({reason}).\n"
        f"The env var names the cluster this process was LAUNCHED BY; the checkout names the "
        f"cluster whose code it is RUNNING. Acting on either one would mix the two clusters "
        f"(one's database, secret and ports; the other's migrations, skills and plugins).\n"
        f"Fix: `unset AVA_HOME` to act on {claimed}, or run the {env_home} cluster's own "
        f"checkout instead of this one. To redirect this checkout on purpose, set "
        f"{_HOME_OVERRIDE}=1."
    )


def resolve_ava_home() -> tuple[Path, bool]:
    """Resolve this process's data-root home and whether it is *anchored*.

    Returns (home, anchored). `anchored` is False only in case 4 below — a
    checkout that claims no cluster and names no home — which boots bare on a
    private scratch home and must never reach another unit's config or gateway.

    Precedence:
        1. AVA_HOME env var -> (that path, True), unless it contradicts the
           checkout's own claim (rule 2 or 3), which raises AvaHomeContradictionError
        2. checkout == ~/.ava/source -> (~/.ava, True)
        3. <checkout>/.ava_home pointer -> (its content, True)
        4. else -> (this process's scratch home, False)

    Rule 1 keeps winning where it is unambiguous — AVA_HOME set on a checkout
    that claims no home of its own (an enrolled runner, a gateway-launched
    daemon, a fresh clone) resolves exactly as before. The one AVA_HOME it does
    not count as a claim is a case-4 scratch home, which `load_ava_env` pins
    into the environment: re-resolving after the boot (the CLI preflight gates
    do), and every child process that inherits it, must still answer
    unanchored. A checkout that claims a home of its own still refuses it as a
    contradiction first.
    """
    env = os.environ.get("AVA_HOME")
    installed = Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    if installed and (not env or not Path(env).is_absolute()):
        raise RuntimeError("installed Ava requires an explicit absolute AVA_HOME")
    if env:
        home = Path(env).expanduser()
        _assert_env_agrees_with_checkout(home)
        return home, not _is_unanchored_scratch(home)
    return checkout_anchored_home()


_HOME, _ANCHORED = resolve_ava_home()
AVA_ENV_PATH = _HOME / ".env"


def checkout_anchored() -> bool:
    """Whether this checkout owns the home it resolves to (resolve_ava_home
    rules 1-3), rather than booting bare on a scratch home (rule 4).

    Callers that would write or route through the resolved home gate on this:
    an unanchored checkout (a bare worktree with no `.ava_home` pointer, a
    fresh clone) owns no cluster, so it may not join one, write config, fetch
    cluster config or launch services — and the default home belongs to the
    prod source checkout alone (shared/paths/__init__.py:prod_service_checkout_error).
    """
    return _ANCHORED


# Optional sibling of .env containing explicitly configured package-manager
# index/registry env vars (PyPI / npm / Homebrew). Kept separate
# from .env so `cp .env.example` never clobbers it and the mirror choice is
# orthogonal to the secrets. Absent when no mirror profile is configured.
AVA_MIRROR_ENV_PATH = _HOME / "mirror.env"


def _load_dotenv_layer(path: Path) -> None:
    """Preserve one default Python index across its two environment aliases."""
    inherited_index = os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL")
    load_dotenv(path)
    selected = (
        inherited_index or os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL")
    )
    if selected is not None:
        os.environ["UV_DEFAULT_INDEX"] = selected


def load_ava_env() -> None:
    """Load this process's `$AVA_HOME/.env` (then `mirror.env`) into os.environ.

    Pins AVA_HOME to the resolved home so everything downstream (Settings.ava_home,
    shared.paths) agrees with the `.env` that was loaded. An unanchored checkout
    (rule 4) loads NO file: its home is the process scratch, it boots bare like a
    CI checkout, and UNANCHORED_DB_SENTINEL stands in as AVA_DB_URL so a stray
    connection fails loudly. The authority pass still runs for it, so a cluster
    value inherited from the parent shell that this unit's (nonexistent) `.env`
    does not declare is dropped rather than trusted — EXCEPT the never-drop
    identity exemptions (`_force_also`: AVA_CLUSTER_SECRET, AVA_GATEWAY_URL, the
    health ports, ...), which the deliberate "explicit env wins" rule keeps.
    This authority pass never gates `/api/bootstrap` itself either way — that
    is `checkout_anchored()` / `fetch_bootstrap_config`'s separate job. So an
    unanchored checkout's inherited AVA_CLUSTER_SECRET / AVA_GATEWAY_URL never
    reaches the bootstrap fetch (blocked there), but still reaches an ordinary
    CLI client's own gateway call (`cli/commands/management/config.py:
    _gateway_base`), which reads explicit env by design.

    mirror.env loads last and, like .env, never overrides an already-set key, so
    precedence is: real environment > .env > mirror.env. It is a no-op when the
    file is absent (the common, non-mirror case). UV_DEFAULT_INDEX and
    UV_INDEX_URL represent one setting across these layers; an inherited legacy
    alias is normalized before a lower-priority layer can supply the other alias.
    Additional index settings remain separate and are never merged here.
    """
    global _manifest_finalizer_boot_authorized  # noqa: PLW0603 - per-process boot authority
    from shared.host.env.registry import (
        MANIFEST_CERTIFICATION_FINALIZER_ENV,
        MANIFEST_CERTIFICATION_SECRET_ENV,
    )

    if os.environ.pop(MANIFEST_CERTIFICATION_FINALIZER_ENV, None) == "1":
        _manifest_finalizer_boot_authorized = True
    os.environ.setdefault("AVA_HOME", str(_HOME))
    if _ANCHORED:
        _load_dotenv_layer(AVA_ENV_PATH)
        _load_dotenv_layer(AVA_MIRROR_ENV_PATH)
    else:
        os.environ.setdefault("AVA_DB_URL", UNANCHORED_DB_SENTINEL)
    _enforce_cluster_env_authority()
    # The unit file is necessary for launcher recovery, but must not become an
    # ambient model-child capability whenever a proof-free child boots config.
    # The finalizer ticket is consumed above rather than trusted from `.env`.
    if not _manifest_finalizer_boot_authorized:
        os.environ.pop(MANIFEST_CERTIFICATION_SECRET_ENV, None)
    os.environ.pop(MANIFEST_CERTIFICATION_FINALIZER_ENV, None)


def manifest_certification_secret_from_env_file() -> str:
    """Read the finalizer proof for its targeted launcher projection only.

    Non-finalizer config boot deliberately removes the proof from ``os.environ``.
    Launchers still need the unit-file value to construct the agent-host's
    private environment; this direct read has no environment side effect.
    """
    from shared.host.env.registry import MANIFEST_CERTIFICATION_SECRET_ENV

    value = dotenv_values(AVA_ENV_PATH).get(MANIFEST_CERTIFICATION_SECRET_ENV)
    return value if isinstance(value, str) else ""


def _identity_env_only() -> frozenset[str]:
    """Machine-identity keys a host may legitimately supply via env alone (the
    bootstrap handoff / a remote unit's first start): never dropped when the
    unit's .env does not declare them.

    A small helper (not a module constant) so the exemption set cannot drift
    from its only consumer.
    """
    return frozenset({"AVA_GATEWAY_URL"})


def _launcher_context() -> str | None:
    """The launcher's process profile for this process tree, live or recorded.

    A launcher-spawned daemon or agent carries the live `AVA_PROCESS_PROFILE`
    marker. A CLI entry point pops that marker before dispatch (the CLI is
    settings-full by design) after recording the value as
    `LAUNCHER_PROFILE_ENV_KEY` (cli/main.py `_normalize_process_profile`), so a
    boot pass reached through a CLI still knows the launcher context (#4334).
    None when the tree has no launcher context at all — a plain shell or test
    process — and the projection exemptions stay off.
    """
    return os.environ.get("AVA_PROCESS_PROFILE") or os.environ.get(LAUNCHER_PROFILE_ENV_KEY)


def _is_launcher_runner_projection(value: str | None) -> bool:
    """Whether `value` is the launcher-injected runner DB projection to keep.

    The agent launcher injects an `ava_runner` URL into every agent-profile
    process's environment, and the force loop below refuses to let the unit's
    `.env` owner URL replace it. The drop loop carries the mirror exemption
    (#4334): on a unit whose `.env` does NOT declare AVA_DB_URL (a pure
    agent-runner), popping the injection left settings-lite and CLI paths with
    no DB source at all — the unanchored sentinel and an `UnanchoredHomeError`
    downstream (#4036).

    The scope is deliberately narrow, so the sibling-leak protection keeps its
    full force: agent-launched trees only — the live `AVA_PROCESS_PROFILE=agent`
    marker or the value a CLI entry point recorded before popping it
    (`_launcher_context`; with only the live marker consulted, cli.main's pop
    made this gate unreachable — #4334) — a plain shell's inherited value still
    drops; runner-role URLs only (an inherited owner URL still drops); anchored
    checkouts only (an unanchored dev checkout keeps the sentinel discipline
    that stops it dialing the host home's database — an agent shell must not
    smuggle the host URL into a bare worktree).

    A value `urlsplit` cannot parse is not a projection either: it drops like
    any other unrecognized value — the authority pass never raises on
    environment input.

    The runner-class shape (a write-generation runner login, or a remote
    plane's provider `ava_runner`) mirrors shared/config/data_plane.py
    `_RUNNER_LOGIN`; it is duplicated at this leaf because this module runs
    BEFORE Settings.
    """
    if not value:
        return False
    if _launcher_context() != "agent" or not _ANCHORED:
        return False
    try:
        username = urlsplit(value).username
    except ValueError:
        # A malformed value (e.g. an invalid IPv6 host) is not a projection:
        # the drop loop absorbs it exactly as it did before #3111 — the boot
        # pass must never raise on environment input (review nit on #3111).
        return False
    return _RUNNER_LOGIN.fullmatch(username or "") is not None


def _is_launcher_redis_url(value: str | None) -> bool:
    """Whether `value` is the launcher-supplied Redis URL to keep.

    The AVA_REDIS_URL mirror of `_is_launcher_runner_projection` (#4334): the
    launcher hands every agent-launched tree the cluster's runtime ACL URL, and
    on a unit whose `.env` does NOT declare AVA_REDIS_URL (a pure
    agent-runner) the drop pass removed the process's only Redis source.

    Unlike the DB projection there is no username shape to gate on — the URL
    carries the same identity as the gateway's own (identity is data in the
    URL, and a pre-identity cluster's URL may carry none) — so the gate narrows
    by context instead: an agent-launched tree (live or recorded profile) on an
    anchored checkout, with a URL `urlsplit` parses to a non-empty host. A
    malformed or hostless value drops exactly as before: the authority pass
    never raises on environment input.
    """
    if not value:
        return False
    if _launcher_context() != "agent" or not _ANCHORED:
        return False
    try:
        return bool(urlsplit(value).hostname)
    except ValueError:
        return False


def watcher_runner_env() -> dict[str, str]:
    """Return the launcher's validated data-plane URLs for a watcher session.

    The PTY host inherits the launcher's ambient env, but a watcher must not
    rely on that inheritance for its runner credentials. Only an anchored
    agent launch tree may explicitly forward them. A profile-less process on
    the secured default-home gateway can carry the owner URL after config
    import; refuse that launch before it creates a doomed watcher session.
    Redis always authenticates, so only a named, password-carrying runtime ACL
    URL is forwarded, never the `default` admin user, whatever the bearer.
    """
    db_url = os.environ.get("AVA_DB_URL")
    redis_url = os.environ.get("AVA_REDIS_URL")
    if (
        db_url
        and redis_url
        and _is_launcher_runner_projection(db_url)
        and _is_launcher_redis_url(redis_url)
    ):
        try:
            db_host = urlsplit(db_url).hostname
            redis_parts = urlsplit(redis_url)
            redis_user, redis_password = redis_parts.username, redis_parts.password
        except ValueError:
            db_host = redis_user = redis_password = None
        if db_host and redis_user not in (None, "default") and redis_password:
            carried = (_GENERATION_ENV, _API_TOKEN_ENV)
            return {
                "AVA_DB_URL": db_url,
                "AVA_REDIS_URL": redis_url,
                **{key: os.environ[key] for key in carried if os.environ.get(key)},
            }

    if _HOME.resolve() == (Path.home() / ".ava").resolve() and os.environ.get("AVA_CLUSTER_SECRET"):
        from shared.host.env.bootstrap import config_source_is_local

        if config_source_is_local():
            raise RuntimeError(
                "watcher launch needs an agent-profile process with an ava_runner "
                "AVA_DB_URL and runner AVA_REDIS_URL; this secured default home "
                "cannot supply a validated runner projection. Launch the "
                "watcher from an agent-profile process."
            )
    return {}


def _enforce_cluster_env_authority() -> None:
    """Force this unit's derived env keys from its own `.env`, overriding a polluted parent
    environment.

    `load_dotenv(override=False)` leaves an already-set key untouched, which is right for most
    config (a real env var should win). But for the cluster-isolation keys (health ports,
    db/redis URLs, channels, gateway port/URL, secrets) it is a footgun: if the shell — or a
    watchdog whose own env was inherited from a sibling cluster's context — already carries
    another cluster's value, `.env` cannot correct it, so the session-env allowlist
    (`child_env`, shared/host/env/registry.py) copies the wrong value into the service's session and
    it binds another cluster's port. Re-read the file values and set them authoritatively. On a
    pure agent-runner this runs BEFORE the gateway config fetch
    (`shared.host.env.bootstrap.inject_config_from_gateway`, at Settings build), so a stale cluster fact
    a pre-cutover `.env` still materializes is pushed here and then overridden by the fetched
    value — migration-tolerant by construction.

    The complementary treatment is general: a cluster-scope alias this unit's own `.env` does
    NOT declare is DROPPED from the environment. The drop covers every cluster-scope alias key
    (shared/host/env/registry.py — the field registry's cluster-pinned AND cluster-default aliases,
    163 keys): a pure agent-runner's `.env` carries no cluster data-plane keys (AVA_DB_URL /
    AVA_REDIS_URL / AVA_APP_PORT / ... — they come from the gateway's /api/bootstrap at
    Settings build), so an inherited value — a sibling cluster's .env sourced into the shell,
    e.g. prod's AVA_APP_PORT=3001 — would otherwise stand and leak into every child process
    (pytest, agent shells, scripts) and could be dialed by mistake. Dropping it lets bootstrap
    inject the real value (runner) or the field default apply (a gateway whose .env
    deliberately omits a key).

    One pair of undeclared keys is NOT dropped, in one context: the launcher-injected
    data-plane projections an agent-launched tree carries — the runner DB projection
    (`ava_runner`-shaped URL) and the Redis URL (`urlsplit`-parseable with a host) — on an
    anchored checkout. The context is the live `AVA_PROCESS_PROFILE=agent` marker or the value
    a CLI entry point recorded before popping it (cli/main.py `_normalize_process_profile` →
    `_launcher_context`); with only the live marker consulted, the CLI pop made the exemption
    unreachable and a probe run from an agent child on a pure agent-runner fell back to the
    sentinel (#4334). The force loop above already refuses to let the unit's `.env` owner URL
    replace the DB projection; the drop loop must not revoke either projection — on a unit
    whose `.env` does not declare them (a pure agent-runner), the pop left settings-lite and
    CLI paths with no DB or Redis source at all (`UnanchoredHomeError`; #4036). Every other
    inherited value — owner-shaped DB URLs, plain-shell values, the unanchored checkout's
    sentinel discipline — keeps the original drop behavior.

    Host-scope keys are never in the cluster set (their scope=host fields are per-box facts
    with no bootstrap source: a not-yet-started runner or the test suites supply them from the
    environment alone — AVA_GATEWAY_URL / AVA_CLUSTER_SECRET etc. — and popping them would
    silently un-configure the fetch). The per-unit health ports and the unanchored sentinel are
    likewise outside the cluster set: a co-located second unit (or the e2e suite) states its
    dynamic block via env only, and a fresh dev checkout plants the sentinel deliberately
    before the load, so popping it would silently un-anchor the checkout (Settings would then
    fail on the no-default field instead of failing with the named sentinel).

    The MACHINE-IDENTITY keys (`env_identity_keys()`: the serve-capability flags, machine
    name/description, memory remote) get the same treatment, with one exemption: a value the
    unit's own `.env` declares is forced in, an inherited one is DROPPED. A unit's machine
    identity is a per-unit fact — it belongs in its own `.env` (`ava start` writes it there) or
    its `$AVA_HOME/machine_*` files, never in whatever a parent process happened to inherit.
    The leak that motivated this was real: the gateway host's login shell carries prod's
    `~/.ava/.env` (AVA_MACHINE_SERVE_GATEWAY=true among it), so a watcher child booting an
    isolated $AVA_HOME with no `.env` resolved as a gateway-capable unit —
    `config_source_is_local()` went True, the settings-lite placeholders were skipped, and the
    authority drop then left AVA_DB_URL / AVA_REDIS_URL missing (Settings: Field required).
    Dropping the undeclared flag makes the child fall through to its own files / False, the
    config source stays local-bare, and the leaked flag can never reach an agent runner or
    agent process again. The host-scoped gateway URL key (AVA_GATEWAY_URL) stays exempt for the
    same reason as the host-scope keys above: a remote unit's first start writes it to
    `.env`, but a not-yet-started runner and the test suites supply it from the environment alone, and
    dropping that would silently un-configure the fetch.
    """
    # The force/drop families come from the env registry's projections
    # (shared/host/env/registry.py): cluster-scope and machine-identity aliases
    # derived from Settings metadata. Declared in .env -> force; undeclared ->
    # drop (F-s4-4, Task #856 Phase C, which closed the gap where 130+
    # cluster-default fields were unprotected against a polluted parent).
    from shared.host.env.registry import (
        ADMIN_DATA_PLANE_ALIASES,
        agent_runner_cluster_aliases,
        env_authority_drop_set,
        env_keep_set,
        health_port_env_aliases,
    )

    # `_force_also` is force-if-declared, never dropped when undeclared:
    # - the per-unit health ports and the gateway URL series: host-scope facts
    #   whose dynamic values (e2e, co-located units) arrive by env alone;
    # - AVA_TIMEZONE: a gateway-hosted child (the schedule runner) receives it
    #   from the gateway's spawn env, the cluster's timezone authority; dropping
    #   it fell back to America/Los_Angeles and schedule #3 fired at PT midnight
    #   (2026-08-21). A pure runner re-injects it from /api/bootstrap anyway;
    # - the tempo URLs: converge bakes them into rendered artifacts while
    #   session children receive host-scope facts by forward, so a stale parent
    #   re-rendered the old target (2026-09-14, up{job="tempo"}=0 for ~14 min;
    #   task #3339).
    # AVA_CLUSTER_SECRET is an ordinary cluster-scope key: only the gateway's
    # `.env` declares it, so a remote unit drops an inherited copy (it
    # authenticates with its capability's machine API token instead).
    _force_also = {
        "AVA_SERVICE_PATH",
        "AVA_GATEWAY_URL",
        "AVA_GATEWAY_PORT",
        "AVA_GATEWAY_HEALTH_URL",
        "AVA_FRONTEND_HEALTHCHECK_URL",
        "AVA_TIMEZONE",
        "AVA_TELEMETRY_TEMPO_QUERY_URL",
        "AVA_TELEMETRY_TEMPO_ENDPOINT",
    } | set(health_port_env_aliases().values())
    file_vals = {**dotenv_values(AVA_ENV_PATH), **dotenv_values(AVA_MIRROR_ENV_PATH)}
    role = "gateway" if _is_gateway_process() else "agent"
    keep = env_keep_set(role) | _force_also
    for key in keep:
        val = file_vals.get(key)
        if key == "AVA_DB_URL" and _keeps_injected_db_url(val):
            continue
        # The unanchored sentinel outranks the file: once a process tree carries
        # it (planted by an unanchored boot or an install pre-plant), no `.env`
        # this pass reads may swap a real database URL in behind it. The drop
        # loop's identical guard keeps it on the undeclared side; this one keeps
        # it on the declared side.
        if val is not None and os.environ.get(key) != UNANCHORED_DB_SENTINEL:
            os.environ[key] = val
    # The drop family minus the never-drop exemptions: cluster-scope aliases the
    # unit's .env does not declare, and machine-identity keys it does not declare
    # (the env-suppliable gateway-URL pair stays exempt).
    for key in env_authority_drop_set(role) - _force_also - _identity_env_only():
        if file_vals.get(key) is None and os.environ.get(key) != UNANCHORED_DB_SENTINEL:
            if key == "AVA_DB_URL" and _keeps_undeclared_db_url(os.environ.get(key)):
                # Mirrored force-loop exemption (#4334): the launcher's runner
                # projection is the agent child's DB source; a pure runner's
                # launcher delivers its installed unit login to every class.
                continue
            if key == "AVA_REDIS_URL" and _is_launcher_redis_url(os.environ.get(key)):
                # The Redis mirror (#4334): same launcher context, no username
                # shape to gate on (see `_is_launcher_redis_url`).
                continue
            os.environ.pop(key, None)

    # Gateway profile: drop agent-runner capability keys. They pull agent
    # modules, plugin registrations and API keys into the gateway (+11MB
    # resident) and, by env forwarding, its daemon sessions; /api/bootstrap reads
    # the .env FILE (shared.host.env.runtime_config.read_env_aliases), so it is unaffected.
    if _is_gateway_process():
        for key in agent_runner_cluster_aliases():
            os.environ.pop(key, None)
    if os.environ.get("AVA_PROCESS_PROFILE") == "agent":
        for key in ADMIN_DATA_PLANE_ALIASES:
            os.environ.pop(key, None)
    _deliver_operator_authority(file_vals.get("AVA_DB_URL"))


def _keeps_injected_db_url(file_url: str | None) -> bool:
    """Whether the force loop leaves os.environ's AVA_DB_URL in place.

    The injected runner projection is authoritative for an agent process; the
    drop loop mirrors this exemption (#4334). Any process carrying a launcher-
    delivered write generation for THIS home's endpoint keeps it too: `.env`
    holds only the credential-free endpoint and never overwrites a delivered
    login."""
    return os.environ.get("AVA_PROCESS_PROFILE") == "agent" or _is_delivered_generation(file_url)


def _endpoint_key(url: str | None) -> tuple[int | None, str] | None:
    """(port, database) of a Postgres URL: what distinguishes one home's endpoint
    from a co-located sibling's. None for a missing or unparseable URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        return parts.port, parts.path
    except ValueError:
        return None


def _is_delivered_generation(file_url: str | None) -> bool:
    """Whether os.environ carries a launcher-delivered login for THIS home.

    A delivery is an AVA_DB_URL plus the non-secret generation marker, naming
    the same (port, database) as the home's own `.env` endpoint — so a sibling
    cluster's leaked delivery never survives the authority pass."""
    if not os.environ.get(_GENERATION_ENV):
        return False
    delivered = _endpoint_key(os.environ.get("AVA_DB_URL"))
    return delivered is not None and delivered == _endpoint_key(file_url)


def operator_db_delivery(endpoint: str | None, *, api: bool) -> dict[str, str] | str:
    """This operator process's gateway login on its home as environment, or why
    it holds none (`shared.cluster.authority.operator_environment`). Nothing for
    an unanchored process, a home without a ledger, or over a delivery naming
    this home's `endpoint` that the process already carries."""
    home = _HOME.expanduser().resolve()
    if not _ANCHORED or not endpoint or _is_delivered_generation(endpoint):
        return {}
    if not (home / "db-authority" / "ledger.json").exists():
        return {}
    from shared.cluster.authority import AuthorityRefusedError, operator_environment

    try:
        return operator_environment(home, endpoint, launcher=_launcher_context(), api=api)
    except AuthorityRefusedError as exc:
        return str(exc)


def _deliver_operator_authority(endpoint: str | None) -> None:
    """Give an operator process on a write-generation home its gateway login
    (`operator_db_delivery`). A delivery it carries stays; a refused process
    keeps the credential-free endpoint and records why, so its first dial names it."""
    global _db_authority_refusal  # noqa: PLW0603 — per-process boot authority result
    _db_authority_refusal = None
    if os.environ.get(_GENERATION_ENV):
        return
    delivery = operator_db_delivery(endpoint, api=bool(os.environ.get("AVA_CLUSTER_SECRET")))
    if isinstance(delivery, str):
        _db_authority_refusal = delivery
    else:
        os.environ.update(delivery)


def _keeps_undeclared_db_url(value: str | None) -> bool:
    """Whether the drop pass keeps an AVA_DB_URL the unit's `.env` omits."""
    return _is_launcher_runner_projection(value) or is_delivered_unit_login()


def is_delivered_unit_login() -> bool:
    """Whether os.environ carries exactly this runner home's installed unit
    login (`shared.cluster.authority.unit`) with its generation marker."""
    generation = os.environ.get(_GENERATION_ENV)
    home = _HOME.expanduser().resolve()
    if not _ANCHORED or not generation or not (home / "db-authority" / "unit.json").exists():
        return False
    from shared.cluster.authority.unit import is_delivered_login

    return is_delivered_login(home, os.environ.get("AVA_DB_URL"), generation)


def deliver_unit_authority() -> None:
    """Give a pure agent-runner process its database login and API token before
    the bootstrap fetch (which serves only the credential-free endpoint and
    authenticates with that token).

    A launcher delivery of this home's installed capability is kept. An
    operator process (the `ava` CLI, a script) with no launcher context
    consumes the installed capability only while it runs the home's admitted
    runtime. Anything else keeps the credential-free endpoint and records why,
    so its first dial fails with that reason (`db_authority_refusal`).
    """
    global _db_authority_refusal  # noqa: PLW0603 — per-process boot authority result
    _db_authority_refusal = None
    if not _ANCHORED or is_delivered_unit_login():
        return
    home = _HOME.expanduser().resolve()
    context = _launcher_context()
    if context is not None:
        _db_authority_refusal = (
            f"this {context}-profile agent-runner process was launched without its unit's "
            "database login; only the root launcher delivers it"
        )
        return
    from shared.cluster.authority import AuthorityRefusedError
    from shared.cluster.authority.unit import consume_unit

    try:
        capability = consume_unit(home)
    except (AuthorityRefusedError, ValueError, OSError) as exc:
        _db_authority_refusal = f"no database authority for this agent-runner process: {exc}"
        return
    os.environ["AVA_DB_URL"] = capability.dsn
    os.environ[_GENERATION_ENV] = str(capability.generation.number)
    if capability.api is not None:
        os.environ[_API_TOKEN_ENV] = capability.api.token


def db_authority_refusal() -> str | None:
    """Why this process holds no database login for its write-generation home,
    or None (a login was delivered, or the home keeps no ledger)."""
    return _db_authority_refusal


def _is_gateway_process() -> bool:
    """Whether the current process is a gateway process.

    Checks AVA_PROCESS_PROFILE — an explicit marker set by the process launcher
    (not derived from the unit capability flag). On a single-box setup every
    process inherits AVA_MACHINE_SERVE_GATEWAY=true from .env, so the flag
    alone cannot distinguish the gateway from agent / CLI / daemon processes.
    Only the process that sets AVA_PROCESS_PROFILE=gateway triggers the
    agent-runner key drop.
    """
    return os.environ.get("AVA_PROCESS_PROFILE", "") == "gateway"
