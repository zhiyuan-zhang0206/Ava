"""Autouse guards: keep every test hermetic and off the host.

Each fixture here is autouse and either forbids a real host effect (native helper
converge, os.exec*, the bootstrap fetch, service readiness / health-port probes,
the schedule manager's session backend, the label LLM) or restores process-global
state a test may have leaked (metering wraps, OTLP export, the stdlib logging
bridge and its logger levels, the database-authority refusal; a leaked environment key,
`AVA_HOME` included, is the root leak guard's: `tests/fixtures/leak_guard.py`). Opt-outs are per-test
markers documented on the fixture that honours them.

Order matters and is kept: pytest sets same-scope autouse fixtures up in
registration order, and within one module alphabetically, so this module is
listed after `tests.fixtures.provisioning` (`_clean_state` sorts before `_guard_*`)
in the root `conftest.py`.
"""

import contextlib
import os
import sys
from collections.abc import Generator, Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_incarnation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test's process admission must not become another test's exit identity."""
    from base.native_process import runtime_incarnation

    monkeypatch.setattr(runtime_incarnation, "_child_incarnation", None)


@pytest.fixture(autouse=True)
def suite_is_not_inside_an_exec_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize the ambient session to the CI shape (issue #2331).

    A suite launched from a fleet agent's `execute_code` runs inside the call's
    exec-domain session, so `base.host.proc.hosting_exec_domain` reports it and any
    unrelated test that reaches an in-process lifecycle leg (`ava restart`,
    stop) would refuse — red on an agent box, green
    in CI. A pty-session or login-shell run (the fleet's test convention) never
    sees this. The predicate's own membership behaviour is exercised in spawned
    child processes (`base/host/tests/test_proc.py`), whose sessions are built for
    the case; a lifecycle test that wants the refusal patches this back.
    """
    monkeypatch.setattr("base.host.proc.hosting_exec_domain", lambda: None)


@pytest.fixture(autouse=True)
def _otlp_export_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the test session hermetic: the OTLP dual-write (base.telemetry ->
    base.telemetry.otlp.telemetry_otlp, default ON since the 2026-08-11 stack decision) would
    otherwise fire real OTLP/HTTP requests at 127.0.0.1:4318 from every
    event-emitting test. base/telemetry/otlp/tests/test_telemetry_otlp.py re-enables the
    flag and installs in-memory providers where the OTLP path is under test.
    """
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", False)


@pytest.fixture(autouse=True)
def _telemetry_event_store_off() -> None:
    """Keep the emitter's telemetry_events sink off for the whole test session: the drain
    thread would otherwise write the shared test database from every event-emitting test,
    and a late batch could land in the next test's rows. Tests of the sink and of its
    readers call `event_store.set_enabled(enabled=True)` themselves and switch it off again.
    """
    from base.telemetry import event_store

    event_store.set_enabled(enabled=False)


@pytest.fixture(autouse=True)
def _restore_metering() -> Iterator[None]:
    """Per-test isolation for the process-global `ava` singleton's metering state:
    whatever a test wrapped, the next test sees the bare callables again.

    `agent.extensions.load_extensions()` installs the SDK-usage recorder as part of
    the SDK installation (`ava.sdk_surface.install`), which replaces every public
    `ava.*` callable — plus the `ava.mcps._call_raw` MCP funnel — with a recording
    proxy, and nothing ever put them back. `load_extensions()` is reached directly
    *and* lazily, via `ava/__init__.py:ensure_plugins_loaded` on an `ava.*` miss, so
    merely touching the namespace permanently swapped out the callables every later
    test in that xdist worker would see.

    That is invisible in isolation and only appears when the polluting test lands in
    the same worker — nondeterministic under `-n`, since `--dist load` distributes
    dynamically. It has already cost a night: a test asserting that `install()` wraps
    an unwrapped funnel read `install()`'s correct no-op as a failure, and the merge
    queue's bisect blamed whichever innocent PR shared the batch (issue #83, after
    #82 fixed that one test's symptom by taking its own baseline).

    Restores from the installation that holds the recorder ledger; a test that
    installs the recorders directly restores its own (``install()`` returns the
    ledger). Free for the tests that never metered: with nothing installed there is
    no ledger and nothing runs; the import runs only once `ava` is loaded (a rename
    fails loudly).
    """
    yield
    if "ava" in sys.modules:
        from ava.sdk_surface import install, metering

        current = install.installed()
        if current is not None and current.metered:
            metering.uninstall(current.metered)


@pytest.fixture(autouse=True)
def _stub_label_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn with prompt runs `generate_label_async` in gateway BackgroundTasks, which
    calls `build_chat_model` and really hits the DeepSeek API — polluting network,
    slowing the suite, making it unstable. The autouse stub returns a fake that
    raises RuntimeError; a triggered BackgroundTask just writes the error into
    loguru (label stays NULL, spawn unaffected). `tests/gateway/test_labels.py`
    monkeypatches a new fake LLM, overriding this default.
    """

    def _fake_factory(_model: str, **_: object) -> object:
        class _RaiseLLM:
            async def ainvoke(self, _msgs: object) -> object:
                raise RuntimeError(
                    "label LLM disabled in tests — see tests/fixtures/guards.py:_stub_label_llm"
                )

        return _RaiseLLM()

    # `services.derived.labeler.labeler` is where `generate_label_async` actually resides;
    # ImportError/AttributeError suppress a module or attribute that is not importable.
    with contextlib.suppress(ImportError, AttributeError):
        monkeypatch.setattr("services.derived.labeler.labeler.build_chat_model", _fake_factory)


# The old `_stub_os_cron` patched only pytest; registration leaked in the e2e
# gateway subprocess, where monkeypatch could not reach. The inherited
# `AVA_OS_JOBS_ENABLED=false` setting covers both processes.


def _stub_everywhere(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str, stub: object
) -> None:
    """Replace `module.name` AND every already-imported alias of it.

    Patching only the definition site is a half-guard: `from mod import f` binds
    the function object into the importing module at ITS import time, and that
    alias is what gets called (a caller may hold one for
    `unpause_local_cluster`, `services.supervision.healthchecks.frontend` one for
    `respawn_service`). Nothing is imported to find them — only modules the run
    already loaded are touched.

    **This is not the same job as reaching a name through its owning module**
    (`base.cluster.session_name(...)` — see the rule in
    `docs/conventions/python-conventions.md`). That convention stops a *new* frozen
    alias from being created, so the owner is the only surface and a reader can move
    modules without taking its patch out of reach. It can only apply to code this
    repo writes. This helper covers the aliases that already exist and cannot be
    converted away: third-party modules and existing direct imports may retain a
    function object. The alias scan reads module `__dict__`s only —
    it must not probe `__getattr__` (PEP 562), which can execute arbitrary
    code with side effects (task #3950)."""
    real = getattr(module, name)
    monkeypatch.setattr(module, name, stub)
    for mod in list(sys.modules.values()):
        # Static lookup on purpose — never `getattr(mod, name, None)`: that
        # invokes a module-level `__getattr__` (PEP 562) on every loaded
        # module, and a dynamic surface can run arbitrary code while merely
        # being probed (task #3950: `ava.mcps.__getattr__` formats its "no such
        # server" message by calling the metered `servers()`, so the old probe
        # emitted a burst of `sdk_call` telemetry rows on every test setup and
        # polluted the per-test event mirror). A dynamically-served name is
        # not a frozen alias either — the real object was never bound into the
        # module's dict, which is exactly the surface the rebind below sets on.
        if getattr(mod, "__dict__", {}).get(name, None) is real:
            monkeypatch.setattr(mod, name, stub)


@pytest.fixture(autouse=True)
def _guard_permissions_helper_native_io(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper ancestry is mandatory at runtime, never an implicit test effect.

    OS_JOBS_ENABLED does not gate the permission ancestor. Caller tests must
    explicitly mock helper converge; helper unit tests may exercise real logic
    with their native command seam mocked. Only an intentional native proof may
    opt out, and remains responsible for its exact-home artifact/job cleanup.
    """
    if request.node.get_closest_marker("native_permissions_helper") is not None:
        return
    from services.desktop import permissions_helper
    from services.desktop.permissions_helper import launchd_job, lifecycle

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail(
            "permissions helper native effect forbidden in unit tests: mock the helper "
            "caller or its native command boundary explicitly. Only an isolated native "
            "proof with exact-home cleanup may use @pytest.mark.native_permissions_helper"
        )

    _stub_everywhere(monkeypatch, permissions_helper, "converge", forbidden)
    monkeypatch.setattr(lifecycle, "run_bounded", forbidden)
    monkeypatch.setattr(launchd_job, "run_bounded", forbidden)


@pytest.fixture(autouse=True)
def _guard_schedule_session_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: the schedule API's control paths reach the real session
    backend (`capture`) and wait for a schedule-manager service that no test
    runs (`request_sync` waits for its queued row to be consumed). Under
    `TestClient(app)` that would hit the real backend and stall every start /
    stop / restart for the wait. Neutralize both; the request row itself is still
    written, so API tests can assert it. Tests of the real consume / sync logic
    call the service's functions directly with a faked backend."""

    async def _noop_wait(pool: object, schedule_id: int) -> None:
        return None

    async def _noop_capture(schedule_id: int, lines: int) -> None:
        return None

    monkeypatch.setattr("gateway.schedules.session_control.wait_consumed", _noop_wait)
    monkeypatch.setattr("gateway.schedules.session_control.capture", _noop_capture)


@pytest.fixture(autouse=True)
def _guard_service_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `cli.commands.lifecycle.root_driver.wait_for_service_tree` reports every
    service ready without polling anything.

    The start path's readiness wait is bounded by `SERVICE_READY_TIMEOUT_S` (180 s),
    and a test that reaches it with real probes would sit there for the full bound —
    long enough under `-n auto` for the worker to be declared down, which reports as
    an infrastructure failure rather than as the stubbing gap it is. Reporting no
    unready specs also keeps such a test's `ava start` at rc 0 instead of the
    readiness code.

    Opt out with `@pytest.mark.real_service_readiness_gate` when the wait or the exit
    code it produces is the subject (cli/commands/lifecycle/tests/test_start_readiness_gate.py)."""
    if request.node.get_closest_marker("real_service_readiness_gate"):
        return
    from cli.commands._probe import ReadinessWait

    ready = ReadinessWait((), 0.0, sessions_gone=False)
    monkeypatch.setattr(
        "cli.commands.lifecycle.root_driver.wait_for_service_tree",
        lambda *_a, **_kw: ready,
    )
    # The root-driven path's wait has the same bound and the same reason to be
    # stubbed for tests that are not about it (cli/commands/lifecycle/tests/test_root_driver.py opts
    # out with a module-level `real_service_readiness_gate` marker).
    monkeypatch.setattr(
        "cli.commands.lifecycle.root_driver._wait_for_root_services_ready",
        lambda *_a, **_kw: ready,
    )


@pytest.fixture(autouse=True)
def _guard_health_port_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `ava start`'s pre-bind health-port gate finds nothing.

    The gate dials this unit's daemon `/healthz` ports before launching (issue
    #977). It does NOT reach the prod defaults (the health ports of the fixed table): the env block in
    `tests.fixtures.env_bootstrap` already pins every health port to a `_free_port()`, so an
    un-stubbed gate dials this session's own kernel-assigned ports and the suite is
    green with prod live. This fixture buys the weaker, residual property.

    `_free_port()` releases the port before anything binds it — the same race
    `tests/_containers.py` accepts for pg/redis. Nothing in a test run ever binds
    these particular ports, so the window is the whole session, and an unrelated
    process that grabs one and answers 2xx on `/healthz` would be read as
    PORT_TAKEN: a start test failing for a reason no test caused. Rare, but the
    stub costs one line and removes the flake source entirely.

    Opt out with `@pytest.mark.real_health_port_gate` when the gate itself is the
    subject (cli/commands/lifecycle/tests/test_start_health_port_gate.py)."""
    if request.node.get_closest_marker("real_health_port_gate"):
        return
    monkeypatch.setattr("cli.commands._probe._occupied_health_ports", lambda *_a, **_kw: ())


@pytest.fixture(autouse=True)
def _test_homes_get_their_own_ports(
    monkeypatch: pytest.MonkeyPatch, session_ports: dict[str, int]
) -> None:
    """Autouse: a home born inside a test records this session's own ports.

    `base.cluster.new_home_ports` is where birth takes the fixed port table
    (`cli.start_identity._new_record`), and those are the numbers the operator's
    cluster on the same box binds. A test home that recorded them could later bring
    up storage, probe or dial the real cluster, and its endpoint would read as the
    real home's (`dotenv_boot._endpoint_key` tells homes apart by port). Every test
    home therefore records the session's kernel-assigned table instead; the table
    itself is pinned by `base/cluster/tests/test_fixed_ports.py`.

    The patch covers this process only: a subprocess that births a home would take
    the real table (no test does)."""
    monkeypatch.setattr("base.cluster.new_home_ports", lambda: dict(session_ports))


# ── Exec guard: no test replaces the pytest process image ──
#
# `os.exec*` does not raise, does not unwind, and does not run a finalizer — it
# overwrites this process with a new program. A test that reaches one takes the
# whole run with it: pytest never reaches `pytest_sessionfinish`, never prints a
# summary, and the shell sees whatever the *replacement* program exited with.
# There is no failure report, so a full-suite run silently under-reports every
# test that had not run yet.
#
# That is not hypothetical (pre-2026-08-01): `cli.start_refresh.
# refresh_runner_env_or_die` ran from `cli.main.main()` before dispatch on
# `ava start`, and on an enrolled runner it fetched /api/bootstrap, rewrote the
# real ~/.ava/.env, and re-exec'd — replacing the pytest process mid-run.
# `cli/tests/test_main_dispatch.py` stubbed it, and the exec guard here is the
# backstop that turns a forgotten stub into a plain test failure instead of a
# vanished run. (The refresh is gone; `ava start`'s Settings build fetches in-
# process, which is what `_guard_bootstrap_fetch` below blocks.)
#
# Sibling shape to watch for: the guard is what makes the next one a plain test
# failure instead of a vanished run.


@pytest.fixture(autouse=True)
def _guard_process_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: the `os.exec*` family fails the test loudly instead of
    replacing the pytest process.

    Patching the four `execv*` names covers all eight — stdlib `os.execl*` are
    thin wrappers that call the module-global `execv`/`execvp`. Real in-process
    exec is never intended from a test: the production call sites are either a
    dedicated `__main__` that runs in a subprocess (`base.native_process.reparent`,
    `services.desktop.browser.daemon`) or a CLI re-exec. Tests
    that assert an exec *would* have happened patch `os.exec*` themselves
    inside the test body — last-write-wins over this default, restored LIFO at
    teardown.

    `os._exit` is deliberately NOT guarded: it is the correct call in a forked
    child (`base.native_process.reparent`), and hijacking it there would resurrect a pytest
    process inside the fork.
    """

    def _refuse(name: str):  # type: ignore[no-untyped-def]
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                f"os.{name}() would replace the pytest process image — the run would end "
                f"here with no summary and no failure report. Stub the code path that "
                f"execs (see cli/tests/test_main_dispatch.py), or patch os.{name} in the "
                f"test body if the exec is the subject under test."
            )

        return _boom

    for _name in ("execv", "execve", "execvp", "execvpe"):
        monkeypatch.setattr(os, _name, _refuse(_name))


@pytest.fixture(autouse=True)
def _guard_bootstrap_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: no test performs a real `GET /api/bootstrap` against a
    live gateway.

    The guard above stops an exec, but the fetch happens first: a pure
    agent-runner's Settings build calls `inject_config_from_gateway()` at
    import, and on an enrolled dev box that is a live GET /api/bootstrap
    against the real cluster gateway. This guard sits at the cause. A runner's
    first start also funnels through `base.host.env.bootstrap.fetch_bootstrap_config`
    (the only caller of this module's `dial_get`); nothing is written until that
    fetch returns, so refusing the dial blocks the whole chain: no request to
    the gateway, no joined state.

    That chain is not theoretical. On an enrolled runner — serve_gateway unset,
    gateway URL in the real `~/.ava/.env`, which is every dev box joined to a
    cluster — a test that constructs Settings without the suite's
    AVA_CONFIG_FETCH=skip pin would fetch from that cluster's live gateway.
    The suite redirects `AVA_HOME` to tmpfs and pins the fetch skip in the
    env block above; this guard is the second net for any test that drives a
    Settings build directly.

    Narrow by construction: `base.host.env.bootstrap` binds `dial_get` at module level,
    so only the bootstrap egress is touched — the ~30 other `base.host.net.http_dial`
    call sites are untouched. Every test that legitimately drives the fetch
    already substitutes its own transport at exactly this seam
    (`base/host/env/tests/test_bootstrap_fetch.py` routes it through an in-process
    TestClient; the retry tests hand it a fake), and those patches win by LIFO.
    """
    import base.host.env.bootstrap

    def _boom(url: object = "", *_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            f"a real GET {url} would leave the test process — this is the call that "
            "reaches a live gateway and, on an enrolled runner, rewrites the operator's "
            "~/.ava/.env. Stub the caller, or patch base.host.env.bootstrap.dial_get in the test "
            "body with a fake transport (see base/host/env/tests/test_bootstrap_fetch.py)."
        )

    monkeypatch.setattr(base.host.env.bootstrap, "dial_get", _boom)


@pytest.fixture(autouse=True)
def _restore_db_authority_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that drives a boot pass (`load_ava_env`, a runner's bootstrap
    injection) may record a per-process database-authority refusal; restore the
    suite's value afterwards so the refusal never leaks into later tests' dials."""
    from base.host.env import dotenv_boot

    monkeypatch.setattr(dotenv_boot, "_db_authority_refusal", dotenv_boot._db_authority_refusal)


# Loggers `base.log._install_stdlib_intercept()` gives a level of its own besides the
# first-party names and the root: the psycopg pool gate and the uvicorn trio.
_INTERCEPT_LEVELED_THIRD_PARTY = ("psycopg.pool", "uvicorn", "uvicorn.error", "uvicorn.access")


@contextlib.contextmanager
def stdlib_logging_isolated() -> Generator[None]:
    """Run a block with the loguru->telemetry bridge off the root logger, then put stdlib
    logging back as it was: the root handlers AND the level of every logger the intercept
    sets.

    `_install_stdlib_intercept()` sets the root to INFO and each first-party logger to
    DEBUG (plus the psycopg pool and uvicorn loggers). Levels live on the process-global
    `logging.Logger.manager`, so restoring only the handlers left them set for every later
    test in the same xdist worker. A stdlib call whose format string cannot consume its
    arguments raises out of pytest's capture handler only when the record passes the level
    gate, so the leak made such a bug fail only in the runs where an intercept test had
    come earlier in the worker.
    """
    import logging

    from base.log import _FIRST_PARTY_LOGGER_NAMES, _StdlibInterceptHandler

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_levels = [
        (logger, logger.level)
        for logger in (
            root,
            *(
                logging.getLogger(name)
                for name in (*_FIRST_PARTY_LOGGER_NAMES, *_INTERCEPT_LEVELED_THIRD_PARTY)
            ),
        )
    ]
    root.handlers = [h for h in saved_handlers if not isinstance(h, _StdlibInterceptHandler)]
    try:
        yield
    finally:
        root.handlers = saved_handlers
        for logger, level in saved_levels:
            logger.setLevel(level)


@pytest.fixture(autouse=True)
def _no_stdlib_telemetry_bridge() -> Iterator[None]:
    """Keep stdlib logging off the loguru->telemetry bridge during tests.

    base/log/__init__.py's `_install_stdlib_intercept()` (called by init_gateway_process
    and friends) installs a root-logger handler that forwards stdlib records
    into loguru, whose `_postgres_sink` turns them into telemetry 'log' events.
    Any test that monkeypatches `base.telemetry.emit` and triggers a log
    record then sees the bridge's forwarded 'log' event in its captured events
    (pgbouncer healthcheck tests, fixed 2026-08-09 via #2136). Tests that
    exercise the bridge itself re-install it inside their body
    (test_uvicorn_stdlib_intercept.py): the fixture runs first, the body's
    install wins for the duration, and teardown restores the saved handlers and
    the logger levels the install changed (`stdlib_logging_isolated`).
    """
    with stdlib_logging_isolated():
        yield
