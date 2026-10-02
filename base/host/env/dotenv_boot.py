"""Single .env load entry — every process entry point imports this.

The `.env` source lives under this unit's home: `$AVA_HOME/.env`. The home is
`$AVA_HOME` when that variable is set, else `~/.ava`. `resolve_ava_home` reads
the variable every time it is called, so no module captures the home at import
and the variable is the only source: there is no checkout pointer, no
per-process override and no second in-process channel, so a process and its
children cannot disagree about their home. No `.env` lives in the repo root.

Production does not depend on the variable. Only a process tree that must not
touch the host's cluster sets it, once, at its top: the test session, and the
hooks and tools that import application code. Every descendant inherits it.

What keeps development code off the host's cluster is that rule plus the
checkout guard (`home_checkout_error`): a home that carries its own
`<home>/source` checkout is operated only by that checkout's code.

Callers:
    base/config.py            - before importing Settings
    scripts/start_agent.py      - bootstrap root agent

`load_dotenv` itself is idempotent + does not overwrite already-set
os.environ keys; repeated calls have no side effects.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv

# Planted as AVA_DB_URL when a boot has no cluster connection facts (the lite
# boot's placeholders, `base/config/_lite.py`). A syntactically valid URL that
# can never reach a real database (port 1 on loopback), so a stray connection
# fails loudly instead of silently hitting a database a host `.env` points at.
# base/db.connect() detects it and raises an actionable error.
PLACEHOLDER_DB_URL = "postgresql://no-cluster@127.0.0.1:1/run-ava-start-first"

# The launcher's process profile, recorded by the CLI entry point before it
# clears the live marker (cli/main.py `_normalize_process_profile`). The CLI is
# deliberately settings-full — no profile — but a boot pass reached through it
# must still see which launcher context this process tree descends from; the
# launcher-projection exemptions in `_enforce_cluster_env_authority` read it
# live-or-recorded (`launcher_context`). Never cleared: a nested CLI only
# overwrites it when it carries a fresh live marker of its own (#4334).
LAUNCHER_PROFILE_ENV_KEY = "AVA_LAUNCHER_PROFILE"

# See `_is_launcher_runner_projection`.
_RUNNER_LOGIN = re.compile(r"ava_runner|ava_g(?:0|[1-9][0-9]*)_runner")

# The non-secret launch-environment marker that makes a launcher-injected
# AVA_DB_URL authoritative (mirrors base.cluster.authority.GENERATION_ENV),
# and the delivered machine API token (base.cluster.authority.api).
_GENERATION_ENV = "AVA_DB_GENERATION"
_API_TOKEN_ENV = "AVA_API_TOKEN"  # noqa: S105 — env key name, not a credential

# Why this process holds no database authority, when a home with a write-
# generation ledger delivered none to it: `base.db.connections._guard_db_url`
# turns a dial of the credential-free endpoint into a named refusal.
_db_authority_refusal: str | None = None

# A finalizer receives this one-use launch ticket alongside its private proof.
# `load_ava_env` consumes it before reading `.env`, so a value planted in the
# file cannot turn an arbitrary model child into a finalizer.
_manifest_finalizer_boot_authorized = False


def resolve_ava_home() -> Path:
    """This process's home: `$AVA_HOME` when set, else `~/.ava`.

    Read on every call — never cached — so a caller that changes the variable
    (a test session, a tool that sets it once at its top) is followed by every
    later call. The path is neither created nor checked here
    (`base.paths.ava_home` creates it).

    Raises:
        ValueError: `$AVA_HOME` is not an absolute path; a relative home would
            follow the working directory.
    """
    env = os.environ.get("AVA_HOME")
    if not env:
        return Path.home() / ".ava"
    home = Path(env).expanduser()
    if not home.is_absolute():
        raise ValueError(f"AVA_HOME must be an absolute path, got {env!r}")
    return home


def home_checkout_error(repo: Path) -> str | None:
    """Why `repo` may not operate this process's home, or None when it may.

    A home that carries its own `<home>/source` checkout (the production home
    `~/.ava`, and every unit started from source) is operated only by that
    checkout's code: a disposable checkout driving it would bind its daemons to
    code that disappears with the checkout (the 01:13 worktree accident, Task
    #966) or apply un-reviewed migrations to its database. A home with no
    `source` (a test session's temporary home, a scratch `AVA_HOME`) accepts any
    checkout.

    Both sides are compared resolved, so a symlinked or `..`-spelled path to
    the same directory is the same checkout.
    """
    home = resolve_ava_home()
    source = home / "source"
    if not source.is_dir() or repo.resolve() == source.resolve():
        return None
    return (
        f"this checkout ({repo}) is not the checkout of the home it would act on: "
        f"{home} carries its own source checkout ({source}), and only that "
        f"checkout may operate it. Run {source}/.venv/bin/ava (the bare `ava` of a "
        f"production host) instead, or set AVA_HOME to a home of your own (a "
        f"temporary directory, for a test or a tool)."
    )


def skip_config_fetch() -> None:
    """Keep this process from fetching cluster configuration from a gateway.

    For a tool that must keep the real home (it reads this machine's own state) but
    has no business asking a gateway for anything: the config boot then stays
    settings-lite and dials nothing. Call it before the tool's first application
    import, as a program only (see `enter_scratch_home`).
    """
    from base.host.env.bootstrap import CONFIG_FETCH_ENV, CONFIG_FETCH_SKIP

    os.environ[CONFIG_FETCH_ENV] = CONFIG_FETCH_SKIP


def enter_scratch_home() -> Path:
    """Point this process tree at a fresh temporary home, whatever its environment carries.

    For a script that imports application code but must not touch the host's
    cluster (the scripts git hooks launch): it sets `AVA_HOME` to a new
    private directory and `AVA_CONFIG_FETCH=skip`, so nothing in the tree
    resolves `~/.ava`, reads another home's `.env` or dials a gateway. Call it
    before the tool's first application import, and only when the tool runs as a
    program (`if __name__ == "__main__":`): every descendant inherits the home,
    so a module that enters one while being imported replaces the importing
    process's own. The directory is removed at exit.

    A pytest process refuses: the session home is set before any project import
    and every later test reads it, so a tool imported at collection that entered
    a scratch home would silently redirect the whole worker.
    """
    import atexit
    import shutil
    import sys
    import tempfile

    if "pytest" in sys.modules:
        raise RuntimeError(
            "enter_scratch_home() was called inside a pytest process: it would replace the "
            "session's AVA_HOME for every later test. A tool calls it only as a program, "
            'behind `if __name__ == "__main__":`; run the tool in a subprocess to test it.'
        )

    home = Path(tempfile.mkdtemp(prefix="ava-scratch-home-"))
    os.environ["AVA_HOME"] = str(home)
    skip_config_fetch()
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


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

    Pins AVA_HOME to the resolved home, so every descendant inherits it;
    resolution itself never depends on the pin. The authority pass then
    forces the unit's own `.env` declarations over a polluted parent
    environment and drops the cluster values the `.env` does not declare —
    EXCEPT the never-drop identity exemptions (`_force_also`:
    AVA_GATEWAY_URL, the health ports, ...), which the deliberate "explicit env
    wins" rule keeps.

    mirror.env loads last and, like .env, never overrides an already-set key, so
    precedence is: real environment > .env > mirror.env. It is a no-op when the
    file is absent (the common, non-mirror case). UV_DEFAULT_INDEX and
    UV_INDEX_URL represent one setting across these layers; an inherited legacy
    alias is normalized before a lower-priority layer can supply the other alias.
    Additional index settings remain separate and are never merged here.
    """
    global _manifest_finalizer_boot_authorized  # noqa: PLW0603 - per-process boot authority
    from base.host.env.registry import (
        MANIFEST_CERTIFICATION_FINALIZER_ENV,
        MANIFEST_CERTIFICATION_SECRET_ENV,
    )

    if os.environ.pop(MANIFEST_CERTIFICATION_FINALIZER_ENV, None) == "1":
        _manifest_finalizer_boot_authorized = True
    home = resolve_ava_home()
    os.environ.setdefault("AVA_HOME", str(home))
    _load_dotenv_layer(home / ".env")
    _load_dotenv_layer(home / "mirror.env")
    _enforce_cluster_env_authority(home)
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
    from base.host.env.registry import MANIFEST_CERTIFICATION_SECRET_ENV

    value = dotenv_values(resolve_ava_home() / ".env").get(MANIFEST_CERTIFICATION_SECRET_ENV)
    return value if isinstance(value, str) else ""


def _identity_env_only() -> frozenset[str]:
    """Machine-identity keys a host may legitimately supply via env alone (the
    bootstrap handoff / a remote unit's first start): never dropped when the
    unit's .env does not declare them.

    A small helper (not a module constant) so the exemption set cannot drift
    from its only consumer.
    """
    return frozenset({"AVA_GATEWAY_URL"})


def launcher_context() -> str | None:
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
    no DB source at all — the placeholder URL and a `PlaceholderDbUrlError`
    downstream (#4036).

    The scope is deliberately narrow, so the sibling-leak protection keeps its
    full force: agent-launched trees only — the live `AVA_PROCESS_PROFILE=agent`
    marker or the value a CLI entry point recorded before popping it
    (`launcher_context`; with only the live marker consulted, cli.main's pop
    made this gate unreachable — #4334) — a plain shell's inherited value still
    drops; runner-role URLs only (an inherited owner URL still drops).

    A value `urlsplit` cannot parse is not a projection either: it drops like
    any other unrecognized value — the authority pass never raises on
    environment input.

    The runner-class shape (a write-generation runner login, or a remote
    plane's provider `ava_runner`) mirrors base/config/data_plane.py
    `_RUNNER_LOGIN`; it is duplicated at this leaf because this module runs
    BEFORE Settings.
    """
    if not value:
        return False
    if launcher_context() != "agent":
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
    by context instead: an agent-launched tree (live or recorded profile) with a
    URL `urlsplit` parses to a non-empty host. A
    malformed or hostless value drops exactly as before: the authority pass
    never raises on environment input.
    """
    if not value:
        return False
    if launcher_context() != "agent":
        return False
    try:
        return bool(urlsplit(value).hostname)
    except ValueError:
        return False


def watcher_runner_env() -> dict[str, str]:
    """Return the launcher's validated data-plane URLs for a watcher session.

    The PTY host inherits the launcher's ambient env, but a watcher must not
    rely on that inheritance for its runner credentials. Only an agent launch
    tree may explicitly forward them. A profile-less process on
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

    if resolve_ava_home().resolve() == (Path.home() / ".ava").resolve() and os.environ.get(
        "AVA_CLUSTER_SECRET"
    ):
        from base.host.env.bootstrap import config_source_is_local

        if config_source_is_local():
            raise RuntimeError(
                "watcher launch needs an agent-profile process with an ava_runner "
                "AVA_DB_URL and runner AVA_REDIS_URL; this secured default home "
                "cannot supply a validated runner projection. Launch the "
                "watcher from an agent-profile process."
            )
    return {}


def _enforce_cluster_env_authority(home: Path) -> None:
    """Force this unit's derived env keys from its own `.env`, overriding a polluted parent
    environment.

    `load_dotenv(override=False)` leaves an already-set key untouched, which is right for most
    config (a real env var should win). But for the cluster-isolation keys (health ports,
    db/redis URLs, channels, gateway port/URL, secrets) it is a footgun: if the shell — or a
    watchdog whose own env was inherited from a sibling cluster's context — already carries
    another cluster's value, `.env` cannot correct it, so the session-env allowlist
    (`child_env`, base/host/env/registry.py) copies the wrong value into the service's session and
    it binds another cluster's port. Re-read the file values and set them authoritatively. On a
    pure agent-runner this runs BEFORE the gateway config fetch
    (`base.host.env.bootstrap.inject_config_from_gateway`, at Settings build), so a stale cluster fact
    a pre-cutover `.env` still materializes is pushed here and then overridden by the fetched
    value — migration-tolerant by construction.

    The complementary treatment is general: a cluster-scope alias this unit's own `.env` does
    NOT declare is DROPPED from the environment. The drop covers every cluster-scope alias key
    (base/host/env/registry.py — the field registry's cluster-pinned AND cluster-default aliases,
    163 keys): a pure agent-runner's `.env` carries no cluster data-plane keys (AVA_DB_URL /
    AVA_REDIS_URL / AVA_APP_PORT / ... — they come from the gateway's /api/bootstrap at
    Settings build), so an inherited value — a sibling cluster's .env sourced into the shell,
    e.g. prod's AVA_APP_PORT=3001 — would otherwise stand and leak into every child process
    (pytest, agent shells, scripts) and could be dialed by mistake. Dropping it lets bootstrap
    inject the real value (runner) or the field default apply (a gateway whose .env
    deliberately omits a key).

    One pair of undeclared keys is NOT dropped, in one context: the launcher-injected
    data-plane projections an agent-launched tree carries — the runner DB projection
    (`ava_runner`-shaped URL) and the Redis URL (`urlsplit`-parseable with a host). The
    context is the live `AVA_PROCESS_PROFILE=agent` marker or the value
    a CLI entry point recorded before popping it (cli/main.py `_normalize_process_profile` →
    `launcher_context`); with only the live marker consulted, the CLI pop made the exemption
    unreachable and a probe run from an agent child on a pure agent-runner fell back to the
    placeholder URL (#4334). The force loop above already refuses to let the unit's `.env` owner URL
    replace the DB projection; the drop loop must not revoke either projection — on a unit
    whose `.env` does not declare them (a pure agent-runner), the pop left settings-lite and
    CLI paths with no DB or Redis source at all (`PlaceholderDbUrlError`; #4036). Every other
    inherited value — owner-shaped DB URLs, plain-shell values — keeps the original drop
    behavior.

    Host-scope keys are never in the cluster set (their scope=host fields are per-box facts
    with no bootstrap source: a not-yet-started runner or the test suites supply them from the
    environment alone — AVA_GATEWAY_URL / AVA_CLUSTER_SECRET etc. — and popping them would
    silently un-configure the fetch). The per-unit health ports and the placeholder DB URL are
    likewise outside the cluster set: the e2e suite states its dynamic block via env only, and a
    process tree carrying the placeholder keeps it, so popping it would replace the named
    refusal with a no-default Settings failure.

    The MACHINE-IDENTITY keys (`env_identity_keys()`: the serve-capability flags, machine
    name/description, memory remote) get the same treatment, with one exemption: a value the
    unit's own `.env` declares is forced in, an inherited one is DROPPED. A unit's machine
    identity is a per-unit fact — it belongs in its own `.env` (`ava init` writes it there),
    never in whatever a parent process happened to inherit.
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
    # (base/host/env/registry.py): cluster-scope and machine-identity aliases
    # derived from Settings metadata. Declared in .env -> force; undeclared ->
    # drop (F-s4-4, Task #856 Phase C, which closed the gap where 130+
    # cluster-default fields were unprotected against a polluted parent).
    from base.host.env.registry import (
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
    file_vals = {**dotenv_values(home / ".env"), **dotenv_values(home / "mirror.env")}
    role = "gateway" if _is_gateway_process() else "agent"
    keep = env_keep_set(role) | _force_also
    for key in keep:
        val = file_vals.get(key)
        if key == "AVA_DB_URL" and _keeps_injected_db_url(val):
            continue
        # The placeholder outranks the file: once a process tree carries it
        # (planted by a lite boot), no `.env` this pass reads may swap a real
        # database URL in behind it. The drop loop's identical guard keeps it on
        # the undeclared side; this one keeps it on the declared side.
        if val is not None and os.environ.get(key) != PLACEHOLDER_DB_URL:
            os.environ[key] = val
    # The drop family minus the never-drop exemptions: cluster-scope aliases the
    # unit's .env does not declare, and machine-identity keys it does not declare
    # (the env-suppliable gateway-URL pair stays exempt).
    for key in env_authority_drop_set(role) - _force_also - _identity_env_only():
        if file_vals.get(key) is None and os.environ.get(key) != PLACEHOLDER_DB_URL:
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
    # the .env FILE (base.host.env.runtime_config.read_env_aliases), so it is unaffected.
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
    it holds none (`base.cluster.authority.operator_environment`). Nothing for
    a home without a ledger, or over a delivery naming this home's `endpoint`
    that the process already carries."""
    home = resolve_ava_home().expanduser().resolve()
    if not endpoint or _is_delivered_generation(endpoint):
        return {}
    if not (home / "db-authority" / "ledger.json").exists():
        return {}
    from base.cluster.authority import AuthorityRefusedError, operator_environment

    try:
        return operator_environment(home, endpoint, launcher=launcher_context(), api=api)
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
    login (`base.cluster.authority.unit`) with its generation marker."""
    generation = os.environ.get(_GENERATION_ENV)
    home = resolve_ava_home().expanduser().resolve()
    if not generation or not (home / "db-authority" / "unit.json").exists():
        return False
    from base.cluster.authority.unit import is_delivered_login

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
    if is_delivered_unit_login():
        return
    home = resolve_ava_home().expanduser().resolve()
    context = launcher_context()
    if context is not None:
        _db_authority_refusal = (
            f"this {context}-profile agent-runner process was launched without its unit's "
            "database login; only the root launcher delivers it"
        )
        return
    from base.cluster.authority import AuthorityRefusedError
    from base.cluster.authority.unit import consume_unit

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
