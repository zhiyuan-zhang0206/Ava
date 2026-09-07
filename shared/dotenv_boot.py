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
    4. else                      - ~/.ava, but UNANCHORED

Rule 1 beating rules 2-3 is only safe while they agree. When they disagree the
process has two different clusters' facts in hand — the env var's `.env`
(database URL, secret, ports) and the checkout's code (`migrations/`, skills,
plugin images) — and there is no defensible way to pick one, because whichever
side wins, the other half of the process's world comes from the loser. So a
contradiction raises `AvaHomeContradictionError` instead of resolving (see
`resolve_ava_home`); `AVA_HOME_OVERRIDE` opts out for callers that mean it.

Case 4 is a dev checkout that was never `ava start`'d and carries no
explicit AVA_HOME. Loading the host `.env` would silently point the process at
the prod database; instead `load_ava_env` plants `UNANCHORED_DB_SENTINEL` as
AVA_DB_URL so any DB connection fails loudly (shared/db.connect raises an
actionable error) rather than writing to prod. This mirrors the same sentinel
tests/conftest.py plants for unprovisioned test runs.

Callers:
    shared/config.py            - before importing Settings
    scripts/start_agent.py      - bootstrap root agent

`load_dotenv` itself is idempotent + does not overwrite already-set
os.environ keys; repeated calls have no side effects.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Planted as AVA_DB_URL when home resolution falls back to an unanchored dev
# checkout. A syntactically valid URL that can never reach a real database (port
# 1 on loopback), so a stray connection fails loudly instead of silently hitting
# the prod database the host .env points at. shared/db.connect() detects it and
# raises an actionable error directing the operator to `ava start`.
UNANCHORED_DB_SENTINEL = "postgresql://unanchored-dev-checkout@127.0.0.1:1/run-ava-start-first"

# Never-dialed Settings placeholder for AVA_REDIS_URL on a not-yet-born install
# home (port 1 on loopback, mirroring the DB sentinel). cli/install_cluster.py
# plants it before the first settings import; the authority pass preserves it
# exactly like the DB sentinel (F-s4-6), so the documented mechanism holds —
# before the S4 fix the drop loop popped it immediately and the comment claimed
# a mechanism that did not exist.
BOOT_REDIS_PLACEHOLDER = "redis://install-cluster-boot@127.0.0.1:1/0"

# Values the authority pass must never drop: both are boot-time placeholders a
# process plants in ITS OWN environment before Settings constructs, and a
# not-yet-born home has no .env to declare them in. Everything else in
# cluster-scope aliases the unit's .env does not declare are dropped so a
# polluted parent cannot leak a sibling cluster's value.
_UNANCHORED_PLACEHOLDERS = frozenset({UNANCHORED_DB_SENTINEL, BOOT_REDIS_PLACEHOLDER})

_HOME_POINTER = ".ava_home"

# Opt out of the AVA_HOME-vs-checkout contradiction check (`resolve_ava_home`).
# For callers that redirect a checkout to a home it does not own ON PURPOSE and
# accept running that checkout's code against it — the test suite's scratch home
# (tests/conftest.py) and `install.sh --worktree` re-pointing a checkout at a new
# cluster. Only the real process environment can open it: the check runs at this
# module's import, before any `.env` is loaded, so no cluster can grant itself
# the exemption on disk.
_HOME_OVERRIDE = "AVA_HOME_OVERRIDE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The 2026-07-06 affirmative-naming refactor (#315) renamed skip_auth_middleware
# -> auth_middleware_enabled and skip_security_scan -> security_scan_enabled with
# the default INVERTED (False->True). The legacy AVA_SKIP_* keys still resolve
# via AliasChoices, but their values mean the opposite of the new fields — a raw
# pass-through would silently turn "skip the scan" into "scan enabled". The
# boot-time translation below rewrites the legacy value into the canonical key
# (inverted) whenever the legacy key is the active source, so Settings never
# sees an untranslated legacy value. The same bool spellings pydantic itself
# accepts for a bool field; anything else falls through and fails fast at
# Settings construction exactly as before. (legacy key, canonical key)
_LEGACY_INVERTED_BOOL_ALIASES = (
    ("AVA_SKIP_AUTH", "AVA_AUTH_MIDDLEWARE_ENABLED"),
    ("AVA_SKIP_SECURITY_SCAN", "AVA_SECURITY_SCAN_ENABLED"),
)
_BOOL_TRUE = _TRUTHY | {"t", "y"}
_BOOL_FALSE = frozenset({"0", "false", "no", "off", "f", "n"})


def _translate_legacy_skip_aliases() -> None:
    """Translate an inverted-semantics legacy AVA_SKIP_* alias into its canonical
    key in os.environ, value inverted, when the legacy key is the active source.

    Runs after `_enforce_cluster_env_authority`: the authority pass drops
    cluster-scope aliases the unit's .env does not declare (AVA_AUTH_MIDDLEWARE_
    ENABLED / AVA_SECURITY_SCAN_ENABLED are cluster-pinned), so translating
    before it would have the translation popped; the legacy keys themselves are
    not registered aliases and survive the pass. When both names are present the
    canonical one wins (AliasChoices order) and no translation happens. An
    unparseable legacy value is left untouched — pydantic raises its usual
    ValidationError at Settings build rather than being guessed at.
    """
    for legacy, canonical in _LEGACY_INVERTED_BOOL_ALIASES:
        if legacy not in os.environ or canonical in os.environ:
            continue
        token = os.environ[legacy].strip().lower()
        if token in _BOOL_TRUE:
            os.environ[canonical] = "false"
        elif token in _BOOL_FALSE:
            os.environ[canonical] = "true"


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
    """The repo checkout this code lives in (`shared/dotenv_boot.py` -> repo root).

    Anchored to `__file__`, not cwd, so resolution is identical no matter where a
    bare script is launched from (the danger is ad-hoc scripts / subagents whose
    cwd is arbitrary)."""
    return Path(__file__).resolve().parent.parent


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


def checkout_anchored_home() -> tuple[Path, bool]:
    """Resolve the home **this checkout owns**, ignoring the AVA_HOME env var.

    `resolve_ava_home` minus its rule-1 override, i.e. cases 2-4 only. The env
    var says which cluster a process was *launched by*; this says which cluster
    the code on disk *belongs to*. The two diverge exactly when a process
    launched by one cluster runs code from another checkout — every agent
    process inherits `AVA_HOME=~/.ava` from the prod session env, so a bare
    `ava start` inside a dev worktree resolves to the prod home while running
    the worktree's code. `shared.migrations` compares this against the identity
    the DB carries and refuses to migrate on a mismatch.

    `anchored` is False for case 4 (a dev checkout with no pointer), where the
    ~/.ava return value is a fallback rather than a claim of ownership — callers
    proving ownership must treat False as "cannot prove it".
    """
    claim = _checkout_claim()
    if claim is None:
        return Path.home() / ".ava", False
    return claim[0], True


def _assert_env_agrees_with_checkout(env_home: Path) -> None:
    """Refuse when AVA_HOME names a different home than the checkout claims.

    The 2026-07-31 prod wedge (#1059) in one line: a fleet agent ran
    `install.sh --worktree` — which wrote the worktree's `.ava_home` correctly —
    then `cd <worktree> && .venv/bin/ava start` from a shell carrying the prod
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

    Returns (home, anchored). `anchored` is False only in case 4 below — a dev
    checkout with no explicit home — which is the one case that must not silently
    inherit the prod database URL.

    Precedence:
        1. AVA_HOME env var -> (that path, True), unless it contradicts the
           checkout's own claim (rule 2 or 3), which raises AvaHomeContradictionError
        2. checkout == ~/.ava/source -> (~/.ava, True)
        3. <checkout>/.ava_home pointer -> (its content, True)
        4. else -> (~/.ava, False)

    Rule 1 keeps winning where it is unambiguous — AVA_HOME set on a checkout
    that claims no home of its own (an enrolled runner, a gateway-launched
    daemon, a fresh clone) resolves exactly as before.
    """
    env = os.environ.get("AVA_HOME")
    installed = Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    if installed and (not env or not Path(env).is_absolute()):
        raise RuntimeError("installed Ava requires an explicit absolute AVA_HOME")
    if env:
        home = Path(env).expanduser()
        _assert_env_agrees_with_checkout(home)
        return home, True
    return checkout_anchored_home()


def _env_path(ava_home: str | None) -> Path:
    """Resolve a unit's .env path from an explicit AVA_HOME value: `$AVA_HOME/.env`,
    or `~/.ava/.env` when None. Used by callers that hold an explicit home
    (cli/enroll.py writes bootstrap env to AVA_ENV_PATH)."""
    base = Path(ava_home) if ava_home else Path.home() / ".ava"
    return base.expanduser() / ".env"


_HOME, _ANCHORED = resolve_ava_home()
AVA_ENV_PATH = _HOME / ".env"


def checkout_anchored() -> bool:
    """Whether this checkout owns the home it resolves to (resolve_ava_home
    rules 1-3), rather than falling back to the default home (rule 4).

    Callers that would write or route through the resolved home gate on this:
    an unanchored checkout (a bare worktree with no `.ava_home` pointer, a
    fresh clone) must never silently operate the default home's config or
    gateway — that home belongs to the prod source checkout
    (shared/paths.py:prod_service_checkout_error).
    """
    return _ANCHORED


# Optional sibling of .env written by `install.sh --mirror NAME`: a bundle of
# package-manager index/registry env vars (PyPI / npm / Homebrew). Kept separate
# from .env so `cp .env.example` never clobbers it and the mirror choice is
# orthogonal to the secrets. Absent on installs that did not opt into a mirror.
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
    shared.paths) agrees with the `.env` that was loaded. For an unanchored dev
    checkout, plants UNANCHORED_DB_SENTINEL as AVA_DB_URL *before* the load — since
    load_dotenv does not override an already-set key, the prod database URL in the
    host .env can no longer win.

    mirror.env loads last and, like .env, never overrides an already-set key, so
    precedence is: real environment > .env > mirror.env. It is a no-op when the
    file is absent (the common, non-mirror case). UV_DEFAULT_INDEX and
    UV_INDEX_URL represent one setting across these layers; an inherited legacy
    alias is normalized before a lower-priority layer can supply the other alias.
    Additional index settings remain separate and are never merged here.
    """
    os.environ.setdefault("AVA_HOME", str(_HOME))
    if not _ANCHORED:
        os.environ.setdefault("AVA_DB_URL", UNANCHORED_DB_SENTINEL)
    _load_dotenv_layer(AVA_ENV_PATH)
    _load_dotenv_layer(AVA_MIRROR_ENV_PATH)
    _enforce_cluster_env_authority()
    _translate_legacy_skip_aliases()


def _identity_env_only() -> frozenset[str]:
    """Machine-identity keys a host may legitimately supply via env alone (the
    bootstrap handoff / enroll-before-first-start): never dropped when the
    unit's .env does not declare them.

    A small helper (not a module constant) so the exemption set cannot drift
    from its only consumer.
    """
    return frozenset({"AVA_GATEWAY_URL", "AVA_PRIMARY_GATEWAY_URL"})


def _enforce_cluster_env_authority() -> None:
    """Force this unit's derived env keys from its own `.env`, overriding a
    polluted parent environment.

    `load_dotenv(override=False)` leaves an already-set key untouched, which is
    right for most config (a real env var should win). But for the
    cluster-isolation keys (health ports, db/redis URLs, channels, gateway
    port/URL, secrets) it is a footgun: if the shell — or a watchdog whose own
    env was inherited from a sibling cluster's context — already carries another
    cluster's value, `.env` cannot correct it, so the session-env allowlist
    (`child_env`, shared/env_registry.py) copies the wrong value into
    the service's session and it binds another cluster's port.
    Re-read the file values and set them authoritatively. On a pure
    agent-runner this runs BEFORE the gateway config fetch
    (`shared.bootstrap.inject_config_from_gateway`, at Settings build), so a
    stale cluster fact a pre-cutover `.env` still materializes is pushed here
    and then overridden by the fetched value — migration-tolerant by
    construction.

        The complementary treatment is general:
    a cluster-scope alias this unit's own `.env` does NOT declare is DROPPED
    from the environment. The drop covers every cluster-scope alias key
    (shared/env_registry.py — the field registry's cluster-pinned AND
    cluster-default aliases, 163 keys): a pure agent-runner's `.env` carries no
    cluster data-plane keys (AVA_DB_URL / AVA_REDIS_URL / AVA_APP_PORT / ... —
    they come from the gateway's /api/bootstrap at Settings build), so an
    inherited value — a sibling cluster's .env sourced into the shell, e.g.
    prod's AVA_APP_PORT=3001 — would otherwise stand and leak into every child
    process (pytest, agent shells, scripts) and could be dialed by mistake.
    Dropping it lets bootstrap inject the real value (runner) or the field
    default apply (a gateway whose .env deliberately omits a key).

        Host-scope keys are never in the cluster set (their scope=host fields
    are per-box facts with no bootstrap source: a not-yet-enrolled runner or
    the test suites supply them from the environment alone — AVA_GATEWAY_URL /
    AVA_CLUSTER_SECRET etc. — and popping them would silently un-configure the
    fetch). The per-unit health ports and the unanchored sentinel are likewise
    outside the cluster set: a co-located second unit (or the e2e suite)
    states its dynamic block via env only, and a fresh dev checkout plants the
    sentinel deliberately before the load, so popping it would silently
    un-anchor the checkout (Settings would then fail on the no-default field
    instead of failing with the named sentinel).

        The MACHINE-IDENTITY keys (`env_identity_keys()`: the serve-capability
    flags, machine name/description, memory remote) get the same treatment, with
    one exemption: a value the unit's own `.env` declares is forced in, an
    inherited one is DROPPED. A unit's machine identity is a per-unit fact — it
    belongs in its own `.env` (install / `ava enroll` write it there) or its
    `$AVA_HOME/machine_*` files, never in whatever a parent process happened to
    inherit. The leak that motivated this was real: the gateway host's login shell
    carries prod's `~/.ava/.env` (AVA_MACHINE_SERVE_GATEWAY=true among it), so a
    watcher child booting an isolated $AVA_HOME with no `.env` resolved as a
    gateway-capable unit — `config_source_is_local()` went True, the
    settings-lite placeholders were skipped, and the authority drop then left
    AVA_DB_URL / AVA_REDIS_URL missing (Settings: Field required). Dropping the
    undeclared flag makes the child fall through to its own files / False, the
    config source stays local-bare, and the leaked flag can never reach an agent
    runner or agent process again. The host-scoped gateway URL keys
    (AVA_GATEWAY_URL / its deprecated alias AVA_PRIMARY_GATEWAY_URL) stay exempt
    for the same reason as the host-scope keys above: enroll writes them
    to `.env`, but a not-yet-enrolled runner and the test suites supply them
    from the environment alone, and dropping that would silently un-configure
    the fetch.
    """
    # The force/drop data comes from the env registry's projections
    # (shared/env_registry.py — R2 convergence point A): the cluster-scope and
    # machine-identity families, derived from the Settings class metadata, not
    # hand-written snapshots. This module keeps its own exemptions (_force_also
    # below, _identity_env_only, the placeholders) and loop logic unchanged.
    from shared.env_registry import (
        ADMIN_DATA_PLANE_ALIASES,
        agent_runner_cluster_aliases,
        env_authority_drop_set,
        env_keep_set,
        health_port_env_aliases,
    )

    # F-s4-4 (Task #856 Phase C): the force-or-drop loop is driven by the
    # field registry's scope metadata (CLUSTER_SCOPE_ALIASES snapshot in
    # env_registry.py == every cluster-pinned/cluster-default alias), NOT by a
    # hand-maintained DERIVED list + exemption sets. A host-scope key is never
    # in the cluster set (scope=host fields are per-box facts with no bootstrap
    # source: a not-yet-enrolled runner or the test suites supply them from the
    # environment alone, and popping them would silently un-configure the
    # fetch); the unanchored sentinel is preserved (a fresh dev checkout plants
    # it before the load). So the only rule needed is: declared in .env ->
    # force; undeclared -> drop. This closes the pre-Phase-C gap where 130+
    # cluster-default fields (provider keys, system-prompt knobs, ...) were
    # unprotected against a polluted parent environment.
    #
    # Two exemptions carry over from the pre-Phase-C DERIVED set (both are
    # force-if-declared, never dropped-when-undeclared):
    # - the per-unit health ports + the gateway URL series: host-scope facts
    #   whose dynamic values (e2e, co-located units) arrive by env alone, but
    #   whose .env declaration must still win over a leaked sibling value;
    # - AVA_CLUSTER_SECRET: the gateway-auth credential a not-yet-enrolled
    #   runner (or a test subprocess) supplies from env alone before its first
    #   fetch — dropping it would silently un-configure the fetch.
    # AVA_TIMEZONE joins the never-drop family for the same reason as the
    # gateway URL series: a gateway-hosted child (the schedule runner) receives
    # it from the gateway's own spawn env, and the gateway IS the cluster's
    # timezone authority (it resolved the value from this same .env or its own
    # env at boot). Dropping the undeclared key left the runner on the field
    # default America/Los_Angeles — silently, since the 2026-08-12 cluster
    # ruling pins Asia/Shanghai — and schedule #3 fired at PT midnight
    # (2026-08-21). A pure agent-runner is unaffected: the gateway's
    # /api/bootstrap fetch re-injects the authoritative value at Settings build
    # regardless of what its env carried.
    _force_also = {
        "AVA_CLUSTER_SECRET",
        "AVA_GATEWAY_URL",
        "AVA_GATEWAY_PORT",
        "AVA_GATEWAY_HEALTH_URL",
        "AVA_FRONTEND_HEALTHCHECK_URL",
        "AVA_TIMEZONE",
    } | set(health_port_env_aliases().values())
    file_vals = {**dotenv_values(AVA_ENV_PATH), **dotenv_values(AVA_MIRROR_ENV_PATH)}
    role = "gateway" if _is_gateway_process() else "agent"
    keep = env_keep_set(role) | _force_also
    for key in keep:
        val = file_vals.get(key)
        if key == "AVA_DB_URL" and os.environ.get("AVA_PROCESS_PROFILE") == "agent":
            continue
        # The unanchored sentinel outranks the file. AVA_DB_URL is a cluster-scope
        # key, and an unanchored checkout resolves AVA_ENV_PATH to the DEFAULT home
        # — so without this guard the force-assign below reads production's `.env`
        # and overwrites the sentinel `load_ava_env` had just planted, handing a dev
        # checkout the prod database URL. The sentinel survived `load_dotenv`
        # (override=False) and then died here; the drop loop's identical guard could
        # never see it, because this loop had already replaced it. Same condition,
        # same reason, one loop earlier.
        if val is not None and os.environ.get(key) != UNANCHORED_DB_SENTINEL:
            os.environ[key] = val
    # The drop family minus the never-drop exemptions: cluster-scope aliases the
    # unit's .env does not declare, and machine-identity keys it does not declare
    # (the env-suppliable gateway-URL pair stays exempt).
    for key in env_authority_drop_set(role) - _force_also - _identity_env_only():
        if file_vals.get(key) is None and os.environ.get(key) not in _UNANCHORED_PLACEHOLDERS:
            os.environ.pop(key, None)

    # Gateway profile: drop agent-runner capability keys from os.environ.
    #
    # On a gateway-capable unit, $AVA_HOME/.env contains every cluster field
    # (the gateway serves them all via /api/bootstrap), but the gateway
    # process itself has no use for agent-runner capability keys --- they pull
    # in agent modules, plugin registrations, and API keys that bloat the
    # process (+11MB resident) and leak into gateway daemon sessions via
    # session env forwarding. bootstrap_config_values() reads the .env FILE
    # directly (shared.runtime_config.read_env_aliases), so dropping these
    # from os.environ does not affect /api/bootstrap distribution.
    if _is_gateway_process():
        for key in agent_runner_cluster_aliases():
            os.environ.pop(key, None)
    if os.environ.get("AVA_PROCESS_PROFILE") == "agent":
        for key in ADMIN_DATA_PLANE_ALIASES:
            os.environ.pop(key, None)


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
