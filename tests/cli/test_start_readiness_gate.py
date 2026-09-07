"""`ava start`'s exit code carries service readiness.

The defect these cover: the start path waited for its launched services and then
returned regardless, so `rc == 0` meant "the start process exited cleanly" and never
"the services are serving". A human saw the crosses in the status snapshot; a program
saw a zero. Prod paid for the difference (see `cli/commands/_gateway_ready.py`).

The behaviour asserted here, not the configuration:

- a rostered service that never becomes ready makes the start exit
  `SERVICES_NOT_READY_EXIT_CODE`, **after** the snapshot has printed and with the
  offending session named;
- a service that is *skipped* — gated out by `ops.spec._gate_reason`, or
  `--disable-service`-d — is not a service that is *unready*, and cannot fail a start;
- readiness reached late but inside the bound costs a healthy start nothing;
- the boot path, whose retry has no attempt cap, can never see the readiness code on
  any of the three platforms;
- a gateway that observes a live update lease waives the verdict without being told
  to, because the one caller that must opt out cannot on the rollout that ships the
  flag — and the waiver stays scoped to that leg.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import cli.commands as _cli
from cli.commands import start as _start_mod
from cli.commands._repo import ServiceSpec
from ops.service_spec import _GATEWAY
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE, SERVICES_NOT_READY_EXIT_CODE

# Captured at import time, before the autouse fixture shortens the bound for the
# tests that must reach expiry — this is the value a real `ava start` uses.
_REAL_START_BOUND = _start_mod.SERVICE_READY_TIMEOUT_S

# Likewise the stdlib sleep, so a test can assert that nothing in this file replaced
# it (`test_shortening_the_poll_leaves_the_stdlib_sleep_alone`).
_REAL_SLEEP = time.sleep


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSessionBackend:
    """In-memory session backend: launches record, kills succeed, nothing runs."""

    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.created: list[str] = []

    def has_session(self, name: str) -> bool:
        return name in self.alive

    def new_session(self, name: str, _cmd: str, _cwd: object, *, env: object, **_: object) -> bool:
        self.created.append(name)
        return True

    def kill_session(
        self, _name: str, *, graceful: bool = False, expected: bool = False, **_: object
    ) -> tuple[bool, str]:
        return True, "forced"

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self.alive if n.startswith(prefix))


def _spec(service: str) -> ServiceSpec:
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_GATEWAY,
        requires_db=True,  # irrelevant to a probe test
        curl_url="http://localhost:1/",
    )


def _sess(service: str) -> str:
    return f"ava-{service}"


# The readiness wait IS the subject here, so stand the global autouse net down for
# every test in this module (tests/conftest.py:_guard_service_readiness, which would
# otherwise report every service ready and make each assertion vacuous).
pytestmark = pytest.mark.real_service_readiness_gate


@pytest.fixture(autouse=True)
def _hermetic_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce `_cmd_start_body` to the readiness step.

    Every precondition (setup collection, converge, the cluster's pg/redis, machine
    registration, the schema assertion) is stubbed to success, and the gateway
    resolver is pinned to an unreachable stub so the status snapshot cannot dial a
    real gateway. What is left un-stubbed on purpose is the wait itself and the
    status snapshot, because those are what these tests are about.
    """
    monkeypatch.setattr(
        _cli,
        "_collect_setup_values",
        lambda _a: (  # pyright: ignore[reportUnknownArgumentType]
            {
                "machine_name": "test-machine",
                "machine_role": "gateway",
                "memory_remote": "git@github.com:test/AvaMemory.git",
                "gateway_url": "http://test-gateway:8000",
            },
            [],
        ),
    )
    monkeypatch.setattr(_cli, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_register_machine_or_die", lambda _r, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 0)
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    from cli.commands import _session_lifecycle as _session_mod
    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "_ensure_gateway_data_plane", lambda: 0)
    monkeypatch.setattr(_start_mod, "cmd_migrations_apply", lambda: None)
    monkeypatch.setattr(_session_mod, "_ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]
    # The bound: short, and the poll's sleep removed, so a test that must reach expiry
    # does so instantly. The tests that assert "no added latency" fail the sleep
    # outright instead of shortening it.
    #
    # `_probe._poll_sleep`, never `cli.commands._probe.time.sleep` — the latter
    # resolves `time` to the stdlib module and disables sleeping process-wide for
    # every test in this file, which is what made issue #1001's 26 GB run: any
    # product loop of the shape `while time.monotonic() < deadline: ...
    # time.sleep(0.5)` keeps its deadline, loses its throttle, and spins.
    monkeypatch.setattr(_start_mod, "SERVICE_READY_TIMEOUT_S", 0.0)
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    # No deploy owns the cluster unless a test says one does. Pinned rather than left
    # live because the real predicate reads the central `deployment_state` row and
    # this host's sessions, so an un-pinned default would make every assertion in this
    # file depend on whether a rollout happens to be running where the suite runs.
    monkeypatch.setattr(_start_mod, "_update_in_flight", lambda: False)
    # The session backends are faked in-memory: post-switch the real POSIX
    # service backend (native supervisor) cannot run under a faked
    # subprocess.run (the reparent helper would RuntimeError). Orchestration
    # sessions use get_backend() too (S7), so the same fake covers them.
    import shared.session_backend as _sb

    monkeypatch.setattr(_sb, "get_backend", _FakeSessionBackend)
    monkeypatch.setattr(_sb, "get_shell_backend", _FakeSessionBackend)


def _roster(monkeypatch: pytest.MonkeyPatch, entries: tuple[tuple[str, str | None], ...]) -> None:
    """Pin the capability roster `_launch_sessions` reads, as (service, gate reason)."""
    from cli.commands import _session_lifecycle as _session_mod

    annotated = tuple((_spec(name), reason) for name, reason in entries)
    monkeypatch.setattr(_session_mod, "_services_for_roles_annotated", lambda _roles: annotated)  # pyright: ignore[reportUnknownArgumentType]


def _probes(monkeypatch: pytest.MonkeyPatch, ready: set[str]) -> None:
    """Every service in `ready` probes True; every other probes False."""
    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session in ready, "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    # Sessions all alive: an unready service that is still running is the "slow, not
    # dead" case, so the wait must spend its bound rather than exit early.
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]


# ─── a rostered service that never comes up fails the start ───────────────────


def test_unready_service_exits_nonzero_after_printing_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The gateway never passes its probe: non-zero exit, distinguishable code, and
    the snapshot + the offending session name both on the same output.

    Order matters as much as the code. An operator who gets a non-zero rc must be
    able to read *which* service in the run that produced it, so the snapshot is
    asserted to precede the verdict rather than merely to exist."""
    _roster(monkeypatch, (("gateway", None), ("labeler", None)))
    _probes(monkeypatch, ready={"labeler"})

    rc = _cli.cmd_start()

    assert rc == SERVICES_NOT_READY_EXIT_CODE
    out = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]
    combined = out.out + out.err  # pyright: ignore[reportUnknownMemberType]
    assert "→ status" in combined, "the status snapshot must print before a non-zero exit"
    assert _sess("gateway") in combined, "the unready session must be named"
    assert combined.index("→ status") < combined.rindex(_sess("gateway")), (  # pyright: ignore[reportUnknownMemberType]
        "the readiness verdict must come after the snapshot an operator reads for it"
    )
    assert "never became ready" in combined


def test_unready_start_revokes_a_previous_serving_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed start cannot leave an earlier boot's recovery authority live.

    Omitting the attempt reset before service launch would leave the old marker
    serving after the non-zero result and permit the boot-time reconcilers.
    """
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    previous = start_serving.begin_start()
    assert start_serving.mark_serving(previous) is True
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())

    assert _cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE
    assert start_serving.is_serving() is False


def test_ready_start_marks_its_serving_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A healthy start admits recovery for the sessions it just brought up."""
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})

    assert _cli.cmd_start() == 0
    assert start_serving.is_serving() is True


def test_updater_start_records_migration_and_readiness_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart's start leg splits its migration and readiness tail without
    changing the normal `ava start` control path."""
    seen: list[str] = []

    @contextmanager
    def _stage(name: str):
        seen.append(name)
        yield

    monkeypatch.setattr(_start_mod, "updater_stage", _stage)
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})

    assert _start_mod._cmd_start_body(persist_services=False, updater_telemetry=True) == 0
    assert seen == ["migration", "readiness"]


def test_unready_service_code_is_not_the_declined_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness code must not collide with `RESTART_DECLINED_EXIT_CODE`.

    `cmd_restart` returns `_cmd_start_body`'s code straight through, and the detached
    updater's recovery ladders read 3 as "nothing was stopped, this host is still
    serving -- do NOT start over it". A restart that stopped services and came back
    with one not serving is the opposite case: it belongs in the recovery branch, so
    the code has to be above 3 rather than equal to it."""
    assert SERVICES_NOT_READY_EXIT_CODE != RESTART_DECLINED_EXIT_CODE
    assert SERVICES_NOT_READY_EXIT_CODE > RESTART_DECLINED_EXIT_CODE

    # cmd.exe ladder (the only one left, R1-6): `if errorlevel N` is ">= N", so the
    # first rung recovers every code above the declined one -- this one included.
    # (POSIX retired its ladder: the in-process entry returns the code straight
    # through `[session-exit] rc=`, and updater_outcome reads it directly.)
    from ops.cluster_deploy import _restart_recovery_cmd

    cmd = _restart_recovery_cmd()
    assert (
        f"if errorlevel {RESTART_DECLINED_EXIT_CODE + 1} "
        "((python -m cli.commands._updater_stage start"
    ) in cmd
    assert SERVICES_NOT_READY_EXIT_CODE >= RESTART_DECLINED_EXIT_CODE + 1


# ─── a session that never launched is not a readiness question ────────────────


def test_failed_launch_exits_nonzero_and_records_the_session_names(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A session spawn that will not start is louder than an unready probe, not
    quieter: the session is named, the start exits non-zero, and the name is left in
    `$AVA_HOME/last_launch_failures` for the rollout's parent process to read.

    Before this, `_new_session`'s bool was dropped and the start exited 0 — which
    is how a frontend that never came up rode out a rollout as a success."""
    from shared import launch_failures

    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_start()

    assert rc == SERVICES_NOT_READY_EXIT_CODE
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "could not be launched" in combined
    assert _sess("gateway") in combined
    assert launch_failures.take() == [_sess("gateway")]


def test_successful_launch_clears_a_previous_runs_failure_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record describes the LAST start, so a clean one has to erase the previous
    list. Left standing, the next rollout would read a resolved failure and report a
    session that is running."""
    from shared import launch_failures

    launch_failures.record([_sess("gateway")])
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_start() == 0
    assert launch_failures.take() == []


def test_failed_launch_is_waived_with_the_readiness_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-readiness-gate` waives this verdict like any other, because both callers
    that pass it would be harmed by a non-zero: the boot loop retries a non-zero
    forever (`shared/boot_policy.py`), and the rollout reads the names out of the
    record instead."""
    from shared import launch_failures

    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_start(readiness_gate=False) == 0
    assert launch_failures.take() == [_sess("gateway")], (
        "waiving the exit code must not waive the record — it is the rollout's channel"
    )


# ─── skipped is not unready ───────────────────────────────────────────────────


def test_gated_out_service_cannot_fail_the_start(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """`browser-mcp` gated out (no AF_UNIX, a Windows agent-runner) probes False and
    the start still exits 0.

    The gate's roster is not a hand-kept list: it is whatever `_launch_sessions`
    actually launched, which is `ops.spec`'s capability view minus the gated entries.
    So a service legitimately absent on this host never reaches the wait, and adding a
    gate in `ops/spec.py` needs no edit on the start path."""
    _roster(
        monkeypatch,
        (("gateway", None), ("browser-mcp", "no AF_UNIX transport on this platform")),
    )
    _probes(monkeypatch, ready={"gateway"})  # browser-mcp probes False

    rc = _cli.cmd_start()

    assert rc == 0
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "skipped: no AF_UNIX" in combined, "a gated service is shown WITH its reason"
    assert "never became ready" not in combined


def test_disable_service_skip_cannot_fail_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's `--disable-service labeler` is deliberate absence, not failure."""
    _roster(monkeypatch, (("gateway", None), ("labeler", None)))
    _probes(monkeypatch, ready={"gateway"})  # labeler probes False

    assert _cli.cmd_start(disabled_services=("labeler",)) == 0


def test_frontend_is_not_gated_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frontend's ~30-60s `npm run build` is excluded from the wait, so a gateway
    start is never held to it — and therefore cannot fail on it either. Its real state
    still shows in the snapshot."""
    _roster(monkeypatch, (("gateway", None), ("frontend", None)))
    _probes(monkeypatch, ready={"gateway"})  # frontend still building

    assert _cli.cmd_start() == 0


def test_degraded_start_still_unpauses_this_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The idle posture is written BEFORE the readiness wait, so a degraded start
    still unpauses.

    This is what keeps the gate from being able to stall a rollout. A runner's
    self-update ends in `ava start`, and Phase B's poll waits for that host to report
    `paused=false` — from the DB, not from the start's exit code. If the readiness wait
    sat before the write, one straggling `browser` on one runner would hold the poll
    to its bound and put the whole rollout into a settle hold."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())

    assert _cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE
    assert calls and calls[-1] == "idle", "a degraded start must still declare this host serving"


# ─── the healthy path pays nothing ────────────────────────────────────────────


def test_late_but_within_bound_readiness_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow starter that binds its port on the third poll is a healthy start: rc 0,
    and the wait returns on the poll that passes rather than running to the bound."""
    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "SERVICE_READY_TIMEOUT_S", 30.0)
    _roster(monkeypatch, (("gateway", None),))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]

    polls = {"n": 0}

    def _slow(_spec: ServiceSpec) -> _cli.ServiceProbe:
        polls["n"] += 1
        return _cli.ServiceProbe(polls["n"] >= 3, "http", "")

    monkeypatch.setattr(_cli, "_probe_service", _slow)

    assert _cli.cmd_start() == 0
    assert polls["n"] == 3, "the wait must return on the poll that passes, not at the bound"


def test_all_ready_roster_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every probe already passing: the gate adds no latency at all — not one sleep
    interval — so it cannot tax a healthy start.

    Asserted against the wait directly rather than through `cmd_start`, and against
    `_probe._poll_sleep` rather than `time.sleep`. A `pytest.fail` installed on the
    stdlib module fires on a sleep from any *other* step of a full start (a psycopg
    connect retry inside the best-effort last-known-good seed, say), which is not
    what this test is about — and does not reproduce on every host, so it read as
    green locally and red on CI. The seam narrows the assertion to this poll."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(True, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: pytest.fail("a fully-ready roster must not sleep"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli._wait_for_services_ready((_spec("gateway"), _spec("labeler")), 30.0).unready == ()


def test_all_ready_start_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same roster through the whole start path: rc 0, no readiness verdict."""
    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "SERVICE_READY_TIMEOUT_S", 30.0)
    _roster(monkeypatch, (("gateway", None), ("labeler", None)))
    _probes(monkeypatch, ready={"gateway", "labeler"})

    assert _cli.cmd_start() == 0


# ─── --no-readiness-gate ──────────────────────────────────────────────────────


def test_no_readiness_gate_still_prints_but_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The opt-out drops the verdict, never the diagnosis: the unready service is
    still named, so a boot log says what is missing while the retry loop stays out
    of it."""
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())

    rc = _cli.cmd_start(readiness_gate=False)

    assert rc == 0
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert _sess("gateway") in combined
    assert "never became ready" in combined


def test_waived_unready_start_keeps_recovery_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exit-code waiver never substitutes for the readiness boundary."""
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())

    assert _cli.cmd_start(readiness_gate=False) == 0
    assert start_serving.is_serving() is False


def test_live_update_lease_waives_the_gate_on_a_gateway(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A gateway that observes a rollout owning the cluster reports the cross and
    exits 0, without being told to.

    This is the version skew, and it is not hypothetical: prod's 2026-07-30 21:09
    rollout is this exact argv. The orchestration runs from the interpreter that
    imported `cli.commands.update` before the checkout, so the parent is the OLD
    revision and passes `start --persist-services` — it has no `--no-readiness-gate`
    to pass — while the child it spawns is the NEW revision and gates by default. The
    parent read the child's 4 as a failed start and reverted the whole cluster. A flag
    cannot fix the rollout that introduces it; the lease can, because reading it lives
    entirely in the child."""
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())
    monkeypatch.setattr(_start_mod, "_update_in_flight", lambda: True)

    from cli.main import _build_parser, _h_start

    # The old orchestrator's argv, verbatim (`cli/commands/update.py` at 7e571b4).
    rc = _h_start(_build_parser().parse_args(["start", "--persist-services"]))

    assert rc == 0, "a rollout's own local leg must not hand its parent a rollback code"
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert _sess("gateway") in combined, "waiving the verdict must not hide the cross"
    assert "cluster update in progress" in combined, "the waiver must say why it exited 0"


def test_live_update_lease_does_not_waive_on_a_pure_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is cluster-wide, so an agent-runner sees the gateway's rollout too —
    and must still gate.

    The waiver exists for one leg only: the one whose exit code a caller turns into a
    cluster-wide revert. A runner's updater ladder answers this code with an
    idempotent `ava start` that repairs that host and touches nothing else, which is
    the right response and worth keeping."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))
    # A CRITICAL service, so the readiness gate still owns the verdict — the
    # waiver scope is what this test is about, and a non-critical straggler
    # (labeler) would no longer produce the readiness code at all (C2).
    _roster(monkeypatch, (("agent-host", None),))
    _probes(monkeypatch, ready=set())
    monkeypatch.setattr(_start_mod, "_update_in_flight", lambda: True)

    assert _cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE


def test_no_lease_still_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The waiver is scoped to a live deploy, not to gateways. Off-rollout — an
    operator's `ava start`, the watchdog's recovery — a gateway with an unready
    service still exits non-zero, which is the whole point of the gate."""
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())
    monkeypatch.setattr(_start_mod, "_update_in_flight", lambda: False)
    # The session backends are faked in-memory: post-switch the real POSIX
    # service backend (native supervisor) cannot run under a faked
    # subprocess.run (the reparent helper would RuntimeError). Orchestration
    # sessions use get_backend() too (S7), so the same fake covers them.
    import shared.session_backend as _sb

    monkeypatch.setattr(_sb, "get_backend", _FakeSessionBackend)
    monkeypatch.setattr(_sb, "get_shell_backend", _FakeSessionBackend)

    assert _cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE


def test_healthy_start_never_asks_about_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease read is on the failure path only.

    `_update_in_flight` is a central-DB round trip; putting it on every start would
    price a healthy bring-up on the data plane being reachable, and `ava start` is
    what an operator runs when it is not."""
    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready={"gateway"})

    def _fail() -> bool:
        raise AssertionError("a start with nothing unready must not read the update lease")

    monkeypatch.setattr(_start_mod, "_update_in_flight", _fail)

    assert _cli.cmd_start() == 0


def test_cli_flag_reaches_cmd_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public and updater-private start flags reach the start seam unchanged."""
    seen: list[tuple[bool, bool]] = []

    def _start(**kwargs: object) -> int:
        readiness_gate = kwargs["readiness_gate"]
        updater_telemetry = kwargs["updater_telemetry"]
        assert isinstance(readiness_gate, bool)
        assert isinstance(updater_telemetry, bool)
        seen.append((readiness_gate, updater_telemetry))
        return 0

    monkeypatch.setattr(
        _cli,
        "cmd_start",
        _start,
    )

    from cli.main import _build_parser

    parser = _build_parser()
    from cli.main import _h_start

    _h_start(parser.parse_args(["start"]))
    _h_start(parser.parse_args(["start", "--no-readiness-gate"]))
    _h_start(parser.parse_args(["start", "--updater-telemetry"]))

    assert seen == [(True, False), (False, False), (True, True)]


# ─── the boot path, whose retry has no cap, never sees the readiness code ──────


def test_boot_retry_child_command_disables_the_gate() -> None:
    """`ava boot`'s child always carries `--no-readiness-gate`.

    This is the load-bearing assertion of the whole change. `run_boot` retries any
    non-zero code forever with no attempt cap, deliberately (`shared/boot_policy.py`).
    If a readiness code could reach it, a box whose headed Chrome will never launch
    would re-run `ava start` every 60 s forever while otherwise serving — turning a
    cosmetic defect into a host that never finishes booting."""
    from cli.boot_retry import _start_command

    assert "--no-readiness-gate" in _start_command([])
    # Operator flags are still forwarded, and the opt-out is appended rather than
    # replacing them.
    cmd = _start_command(["--machine-name", "h1"])
    assert cmd[-3:] == ["--machine-name", "h1", "--no-readiness-gate"]


def test_boot_retry_loops_on_step_failure_and_stops_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retried set is unchanged: rc 1 (a step failed — the ENETUNREACH class this
    loop exists for) keeps looping, rc 0 ends it."""
    from cli import boot_retry

    codes = iter([1, 1, 0])
    attempts = {"n": 0}

    def _fake_run(_cmd, check=False, **_kw):
        attempts["n"] += 1
        return _FakeResult(returncode=next(codes))

    monkeypatch.setattr(boot_retry.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(boot_retry.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    assert boot_retry.run_boot([]) == 0
    assert attempts["n"] == 3


def test_boot_start_under_the_gate_off_flag_exits_zero_when_a_service_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end for the boot contract: the exact argv `ava boot` runs, on a host
    with a permanently-unready service, exits 0 — so the uncapped loop terminates.

    Asserted through `cmd_start` rather than reasoned about, because the whole risk of
    this change is a boot loop that never ends."""
    from cli.boot_retry import _start_command
    from cli.main import _build_parser, _h_start

    _roster(monkeypatch, (("gateway", None),))
    _probes(monkeypatch, ready=set())

    argv = _start_command([])
    assert argv[2:4] == ["cli.main", "start"]
    args = _build_parser().parse_args(argv[3:])
    assert _h_start(args) == 0


def test_macos_autostart_plist_disables_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS boot job runs `ava start` under launchd rather than `ava boot`, and
    `KeepAlive`/`SuccessfulExit` is a *boolean* — launchd cannot tell rc 1 from the
    readiness code. So the plist must carry the same opt-out, or the three platforms
    stop agreeing on one retry behaviour, which `shared/boot_policy.py` requires."""
    import shared.os_autostart as _autostart

    monkeypatch.setattr(_autostart, "_home_slug", lambda: "ava")
    monkeypatch.setattr(_autostart, "ava_binary_path", lambda: "/usr/local/bin/ava")
    monkeypatch.setattr(_autostart, "launchd_env_block", lambda: "")
    monkeypatch.setattr(_autostart.settings.general, "ava_home", str(tmp_path))

    plist = _autostart._autostart_plist_content()

    assert "<string>start</string>" in plist
    assert "<string>--no-readiness-gate</string>" in plist
    assert plist.index("<string>start</string>") < plist.index(
        "<string>--no-readiness-gate</string>"
    ), "the flag must follow the subcommand, not precede it"
    # The retry itself is untouched: launchd still respawns while the job exits non-zero.
    assert "SuccessfulExit" in plist


# ─── the rollout's local leg answers readiness elsewhere ──────────────────────


def test_gateway_local_update_start_opts_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rollout's local `ava start` passes `--no-readiness-gate`.

    Not because readiness does not matter there but because it is answered one step
    later, better, by `_gateway_ready.await_gateway_serving` — off-box and
    authenticated, through the same probe each runner's preflight uses. Gating the
    child too would nest two waits on overlapping questions, and would route a slow
    `milvus` into `_recover_rc`, rolling the whole cluster back to last-known-good
    over a service Phase B does not depend on."""
    seen: list[list[str]] = []

    def _fake_run(args, **_kw):
        seen.append([str(a) for a in args])  # pyright: ignore[reportUnknownArgumentType]
        return _FakeResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import update as _update

    # On `_update`, not on the `cli.commands` package: `update.py` does `from
    # cli.commands.stop import _do_stop` at import, so it holds its own reference
    # and a stub on the package namespace never reaches this call. It missed
    # silently, and the REAL `_do_stop` ran the whole teardown against the faked
    # `subprocess.run` — every service session answered "still alive" forever, so
    # each one spent the full 15 s of `_graceful_kill_session`'s wall-clock bound
    # appending to `seen`. That is issue #1001's 26 GB test.
    monkeypatch.setattr(_update, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_update, "git_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(_update, "current_schema_state", set)

    _update._run_gateway_local_update(tmp_path, pull=False)

    start_argv = next(a for a in seen if "start" in a)
    assert "--no-readiness-gate" in start_argv
    assert "--persist-services" in start_argv


# ─── recovery paths must not escalate a degraded start ────────────────────────


def test_rollback_keeps_the_rollback_when_a_service_is_unready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ava cluster rollback` must NOT undo itself over an unready service.

    The `start_rc != 0` branch re-applies forward migrations and git-resets back to
    the pre-rollback commit. Firing that because a `browser` was slow would leave the
    operator with neither the state they asked for nor the one they had."""
    from cli.commands import _cluster_rollback as _rb

    undone: list[str] = []

    def sync(_repo: Path, *, timeout_s: float = 600.0) -> _FakeResult:
        del timeout_s
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_rb, "git_reset_hard", undone.append)
    monkeypatch.setattr(_rb, "rollback_schema_to", lambda *_a, **_kw: (True, []))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "run_uv_sync", sync)
    monkeypatch.setattr(_rb, "current_schema_state", set)
    monkeypatch.setattr(_rb, "_migration_set_at_commit", lambda _sha: set())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "git_head_sha", lambda: "f" * 40)

    def _fake_run(args, **_kw):
        argv = [str(a) for a in args]  # pyright: ignore[reportUnknownArgumentType]
        if "start" in argv:
            return _FakeResult(returncode=SERVICES_NOT_READY_EXIT_CODE)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_rb.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    rc = _rb._run_rollback("b" * 40, repo=tmp_path, from_sha="f" * 40)

    assert rc == 0, "a degraded-but-landed rollback is not a failed rollback"
    assert undone == ["b" * 40], (
        "only the forward reset onto the rollback target may run — a reset back to the "
        "pre-rollback commit would mean the rollback was undone"
    )


def test_gateway_recovery_reports_degraded_not_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gateway recovery to last-known-good must not page a human over a slow service.

    `_recover_rc` maps any non-zero from `_recover_gateway_local` onto "gateway DOWN,
    no auto-fix, a human is needed". A recovery start that ran every step and came
    back with one straggling daemon is degraded, not down."""
    from cli.commands import _update_recover as _rec

    def sync(_repo: Path, *, timeout_s: float = 600.0) -> _FakeResult:
        del timeout_s
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_rec, "rollback_schema_to", lambda *_a, **_kw: (True, []))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rec, "git_reset_hard", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rec, "run_uv_sync", sync)

    def _fake_run(args, **_kw):
        argv = [str(a) for a in args]  # pyright: ignore[reportUnknownArgumentType]
        if "start" in argv:
            return _FakeResult(returncode=SERVICES_NOT_READY_EXIT_CODE)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_rec.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    rc = _rec._recover_gateway_local(tmp_path, "c" * 40, set(), preserve_sessions=frozenset())

    assert rc == 0, "degraded recovery must not escalate to 'gateway DOWN'"


# ─── the wait's own contract ───────────────────────────────────────────────────


def test_wait_returns_the_unready_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait reports *which* specs are unready, not just that some are — that list
    is what lets the exit code arrive with the diagnosis attached."""
    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session != "gateway", "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("labeler")), timeout_s=0.0)

    assert [s.session for s in wait.unready] == ["gateway"]


def test_wait_gives_up_early_only_once_every_dead_session_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launched service whose session is gone will never bind, so the wait stops
    instead of spending the bound — but only on the SECOND such reading, because the
    backend may not have registered a just-spawned session yet."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    sleeps = {"n": 0}
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: sleeps.__setitem__("n", sleeps["n"] + 1),  # pyright: ignore[reportUnknownArgumentType]
    )

    wait = _cli._wait_for_services_ready((_spec("gateway"),), timeout_s=600.0)

    assert [s.session for s in wait.unready] == ["gateway"]
    assert sleeps["n"] == 1, "one confirmation interval, not the 600s bound"


def test_wait_does_not_cut_short_a_slow_service_because_a_sibling_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dead session must not end the wait while another service is still alive and
    still coming up — reporting a gateway 20s from serving as unready would make the
    exit code lie in the other direction."""
    monkeypatch.setattr(_cli, "_has_session", lambda sess: sess != _sess("browser"))  # pyright: ignore[reportUnknownArgumentType]
    polls = {"n": 0}

    def _probe(spec: ServiceSpec) -> _cli.ServiceProbe:
        if spec.session == "browser":
            return _cli.ServiceProbe(False, "http", "")  # dead, never coming back
        polls["n"] += 1
        return _cli.ServiceProbe(polls["n"] >= 4, "http", "")  # slow, but coming

    monkeypatch.setattr(_cli, "_probe_service", _probe)
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("browser")), timeout_s=600.0)

    # `browser` is non-critical (C2): its confirmed-dead session leaves the wait
    # without ending it, and it is reported on the alert rail — the live-but-slow
    # gateway still gets its time either way.
    assert wait.unready == ()
    assert [s.session for s in wait.non_critical_unready] == ["browser"]
    assert polls["n"] == 4, "the live-but-slow service was given its time"


def test_probeless_service_can_never_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`browser-mcp` has no HTTP/TCP/pidfile probe — its transport is a Unix socket
    only its healthcheck dials — so it reports `None` and can never be observed
    unready. Absence of evidence must not become a failed start."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(None, "n/a", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: pytest.fail("an n/a probe must not be treated as not-ready"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli._wait_for_services_ready((_spec("browser-mcp"),), timeout_s=5.0).unready == ()


# ─── the verdict names the exit it took, not always the bound ────────────────
#
# The two exits above cost different amounts of time and send the reader to
# different places, and both used to print the same "never became ready within
# 180s". On 2026-07-30 a rollout took the sessions-gone exit in about a second and
# printed 180s; the first diagnosis read that as "the bound was too tight" and went
# looking for a box starved by a concurrent npm build. That hypothesis was not just
# wrong but impossible — the reflog bracketed the whole `ava start` at 60 s — and
# nothing in the output said so (#1016).


def test_the_sessions_gone_exit_reports_elapsed_instead_of_the_unspent_bound(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Every session gone: the wait returned in about a second, so the bound was never
    spent and naming it asserts an elapsed time that the surrounding timestamps
    contradict. Say how long it actually took, and that the sessions are absent —
    which is the next question, and not one a longer timeout answers."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("gateway-watchdog")), 180.0)
    assert wait.sessions_gone is True
    assert wait.elapsed_s < 180.0

    _cli._print_unready_services(wait, 180.0)

    out = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "never became ready within 180s" not in out
    assert "their sessions are gone" in out
    assert f"not ready after {wait.elapsed_s:.1f}s" in out
    # and it says outright that the knob the reader is reaching for is the wrong one
    assert "raising it would change nothing" in out


def test_the_deadline_exit_still_names_the_bound(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The other half: a service alive and still not serving DID spend the bound, so
    the bound is the meaningful number and waiting longer might genuinely help. The
    elapsed time rides along so the two lines are read the same way."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    wait = _cli._wait_for_services_ready((_spec("gateway"),), timeout_s=0.0)
    assert wait.sessions_gone is False

    _cli._print_unready_services(wait, 180.0)

    out = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "never became ready within 180s" in out
    assert "their sessions are gone" not in out
    assert "still not serving" in out


def test_a_mixed_roster_that_runs_out_of_time_is_not_a_sessions_gone_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """One dead session among live ones cannot reach the early exit (it requires ALL
    of them gone), so the wait spends its bound and the verdict must not claim the
    sessions are gone — the live one is exactly the service a longer bound could
    still save."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda sess: sess != _sess("browser"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("browser")), timeout_s=0.0)

    assert wait.sessions_gone is False
    # The verdict is critical-only (C2): `browser` is non-critical and still
    # inside its short window here, so it is neither a failure nor reported.
    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.non_critical_unready == ()
    _cli._print_unready_services(wait, 180.0)
    assert "never became ready within 180s" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_readiness_bound_is_the_shared_constant() -> None:
    """The start path's bound is `shared.deploy_timing`'s, not a literal in the CLI —
    and it is well above the 15 s that prod proved insufficient for one uvicorn."""
    from shared.deploy_timing import GATEWAY_READY_TIMEOUT_S, SERVICE_READY_TIMEOUT_S

    assert _REAL_START_BOUND is SERVICE_READY_TIMEOUT_S
    assert SERVICE_READY_TIMEOUT_S > 15.0
    # Same physical job as the rollout's off-box gate (a local daemon binding a port),
    # so the two are expected to agree in magnitude even though they stay separate
    # constants with separate consumers.
    assert SERVICE_READY_TIMEOUT_S == GATEWAY_READY_TIMEOUT_S


# ─── the poll's sleep is a seam, not the stdlib ───────────────────────────────


def test_shortening_the_poll_leaves_the_stdlib_sleep_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubbing this poll's throttle must not disarm every other sleep in the process.

    `_hermetic_start` above is autouse, so whatever it does to sleeping is in force
    for every test in this file, and this test runs under it: `time.sleep` being the
    real one here is the assertion, made from inside the blast radius.

    The version that patched `cli.commands._probe.time.sleep` resolved `time` to the
    stdlib module and replaced `time.sleep` process-wide. What that breaks is not the
    tests — it is every product loop of the shape `while time.monotonic() < deadline:
    ...; time.sleep(interval)`, which keeps its real wall-clock bound and loses its
    only throttle. `shared.session_backend._graceful_kill_session` is exactly that shape,
    and one test in this file reached it: it spun for the full 15 s per session
    appending to a recorder list, and `pytest tests/cli` peaked at 26 GB on a 16 GB
    box, took swap out from under the prod cluster on it, and stretched agent boots
    from 850 ms to 78-93 s (issue #1001)."""
    import time

    from cli.commands import _probe as _probe_mod

    assert time.sleep is _REAL_SLEEP, (
        "an autouse fixture in this file replaced the stdlib sleep; patch "
        "`cli.commands._probe._poll_sleep` instead"
    )

    monkeypatch.setattr(_probe_mod, "_poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    assert time.sleep is _REAL_SLEEP, "the seam must be module-local"

    # And the seam is the name the poll actually calls, so patching it is not a no-op
    # that leaves the real sleep running (which would make the isolation vacuous).
    slept: list[float] = []
    monkeypatch.setattr(_probe_mod, "_poll_sleep", slept.append)
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    _cli._wait_for_services_ready((_spec("gateway"),), 0.0)
    assert slept == [], "timeout_s=0 crosses the deadline before the first sleep"

    calls = {"n": 0}

    def _flip(_spec):
        calls["n"] += 1
        return _cli.ServiceProbe(calls["n"] >= 2, "http", "")

    monkeypatch.setattr(_cli, "_probe_service", _flip)  # pyright: ignore[reportUnknownArgumentType]
    assert _cli._wait_for_services_ready((_spec("gateway"),), 30.0).unready == ()
    assert slept == [_probe_mod._READY_POLL_INTERVAL_S], (
        "the poll must route its throttle through the seam"
    )


# ─── the tiered gate: critical 180s / non-critical 45s / alert on demotion ─────
#
# C2 (rollout speedup, Task #2183): the 2026-08-30 rollout spent 182 s of its
# 197.5 s local start on a pitr-uploader healthz that never answered — a service
# whose failure nothing downstream depended on. The gate is now tiered: the
# critical roster keeps the full bound, everything else gets a short window and
# then stops blocking the wait — reported and alerted, never silently dropped.


class _FakeClock:
    """Stand-in for `cli.commands._probe.time`: `monotonic()` advances only when
    the poll's sleep says so, so a test can spend a bound instantly and then read
    the exact elapsed time it spent (no real waiting, no patched stdlib)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    from cli.commands import _probe as _probe_mod

    clock = _FakeClock()
    # Rebind only THIS module's `time` name — the stdlib module object stays
    # intact for every other consumer (the same lesson as issue #1001).
    monkeypatch.setattr(_probe_mod, "time", clock)
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        clock.advance,
    )
    return clock


def test_critical_service_manifest_is_pinned_and_real() -> None:
    """The critical roster is an explicit single source of truth: this exact set,
    and every name is a session `ops.spec` actually builds — a rename or a typo
    would silently demote a service to the short window and turn red here."""
    from cli.commands import _probe as _probe_mod
    from ops.spec import services_for_capabilities_annotated

    assert (
        frozenset(
            {
                "gateway",
                "frontend",
                "agent-host",
                "gateway-watchdog",
                "agent-runner-watchdog",
                "im-bridge",
            }
        )
        == _probe_mod.CRITICAL_SERVICE_SESSIONS
    )
    real = {
        s.session
        for s, _reason in services_for_capabilities_annotated(
            frozenset({"gateway", "agent-runner"})
        )
    }
    assert real >= _probe_mod.CRITICAL_SERVICE_SESSIONS, (
        "every critical session must exist in ops.spec"
    )


def test_critical_service_waits_the_full_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """A critical service that never comes up holds the wait for the whole
    `SERVICE_READY_TIMEOUT_S` (180 s): the tier keeps the long bound."""
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 45.0)
    _fake_clock(monkeypatch)

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("pitr-uploader")), 180.0)

    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.elapsed_s == 180.0, "the critical bound must be spent in full"
    assert wait.sessions_gone is False
    assert [s.session for s in wait.non_critical_unready] == ["pitr-uploader"], (
        "the non-critical service left the wait at its own window, long before the critical bound"
    )


def test_non_critical_service_gets_only_the_short_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-critical service that never comes up holds the wait only for
    `NON_CRITICAL_SERVICE_READY_TIMEOUT_S` (45 s): the wait returns with the
    critical roster clean and the straggler in `non_critical_unready`."""
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session == "gateway", "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 45.0)
    _fake_clock(monkeypatch)

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("pitr-uploader")), 180.0)

    assert wait.unready == ()
    assert wait.elapsed_s == 45.0, "the wait must end at the short window, not the bound"
    assert [s.session for s in wait.non_critical_unready] == ["pitr-uploader"]


def test_critical_verdict_ignores_non_critical_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the critical bound expires while a non-critical service is still inside
    ITS window, the verdict is critical-only — the straggler is neither a failed
    start nor a reported failure (it was still being waited on)."""
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 600.0)
    _fake_clock(monkeypatch)

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("pitr-uploader")), 0.0)

    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.non_critical_unready == ()


def test_non_critical_session_gone_exits_without_spending_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-critical service whose session is confirmed gone leaves the wait after
    the same two confirmations as the critical roster — waiting cannot help a dead
    session, and it is reported as failed rather than waited out. The wait does
    not return on the critical roster's clearance alone: the dying session is
    still given its confirmation interval."""
    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session == "gateway", "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    monkeypatch.setattr(
        _cli,
        "_has_session",
        lambda sess: sess == _sess("gateway"),  # pyright: ignore[reportUnknownArgumentType]
    )
    sleeps = {"n": 0}
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: sleeps.__setitem__("n", sleeps["n"] + 1),  # pyright: ignore[reportUnknownArgumentType]
    )
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 600.0)

    wait = _cli._wait_for_services_ready((_spec("gateway"), _spec("pitr-uploader")), 600.0)

    assert wait.unready == ()
    assert [s.session for s in wait.non_critical_unready] == ["pitr-uploader"]
    assert sleeps["n"] == 1, "one confirmation interval, not the 600 s window"


def test_non_critical_failure_posts_alert_and_im(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The tier's second rail end-to-end: a start whose critical roster is healthy
    but a non-critical service is down exits 0 and posts an alerts row + an IM —
    the downgrade is a verdict change, never a silence."""
    import shared.alerts as _alerts
    from cli.commands import _probe as _probe_mod

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def commit(self) -> None:
            pass

    _roster(monkeypatch, (("gateway", None), ("pitr-uploader", None)))
    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session == "gateway", "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 0.0)
    calls: dict[str, list[object]] = {"upsert": [], "im": []}
    monkeypatch.setattr(_probe_mod, "_alert_db_connect", _FakeConn)
    monkeypatch.setattr(_alerts, "display_language", lambda _conn: "en")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_alerts, "notify_text", lambda _a, _l: f"TEXT:{_a['labels']['service']}")  # pyright: ignore[reportUnknownArgumentType]

    def _upsert(
        _conn: object, alert: dict[str, object], source: str
    ) -> tuple[object, bool, bool, dict[str, object]]:
        calls["upsert"].append((alert, source))
        return ("fp", "start"), True, True, {"notified_at": None}

    monkeypatch.setattr(_alerts, "upsert_alert", _upsert)
    monkeypatch.setattr(_alerts, "stamp_notified", lambda _conn, _keys: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_alerts, "fingerprint", lambda _labels: "fp")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _alerts,
        "notify_im",
        lambda _text: calls["im"].append(_text) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    # No open instance yet: the alert DB lookup returns none, so the upsert
    # proceeds as a fresh firing.
    monkeypatch.setattr(_probe_mod, "_unresolved_alert_instance", lambda _c, _s: None)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_start()

    assert rc == 0, "a non-critical failure must not fail the start"
    assert calls["upsert"], "an alerts row must be written"
    alert, source = calls["upsert"][0]  # type: ignore[misc]
    assert source == "start-readiness"
    assert alert["labels"]["service"] == "ava-pitr-uploader"  # type: ignore[index]
    assert calls["im"], "an IM notification must be sent"
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "non-critical" in combined, "the printed verdict must name the tier"


def test_non_critical_alert_failure_degrades_to_a_print(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB/IM failure while posting the alert must not fail the start — the
    printed note in the log is the degraded channel, and rc stays 0."""
    from cli.commands import _probe as _probe_mod

    _roster(monkeypatch, (("gateway", None), ("pitr-uploader", None)))
    monkeypatch.setattr(
        _cli,
        "_probe_service",
        lambda spec: _cli.ServiceProbe(spec.session == "gateway", "http", ""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_probe_mod, "NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 0.0)

    def _boom() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(_probe_mod, "_alert_db_connect", _boom)
    # Both rails degrade to a printed note; the start itself must not fail.
    monkeypatch.setattr(_probe_mod, "_unresolved_alert_instance", lambda _c, _s: None)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_start() == 0


# ─── the alert lifecycle: one instance per failure, resolved on recovery ──────
#
# QA #1196 P1-1: an alert instance keyed by (fingerprint, starts_at) must be
# REUSED while the failure is still open — otherwise every `ava start` (the
# boot job retries every 60 s with no cap) inserts a fresh row and a fresh IM.
# And the instance must be resolved when the service recovers, so the
# Inspector's unresolved panel never shows a fixed failure.


class _FakeAlertConn:
    """In-memory alerts connection for the lifecycle tests: `upsert_alert` rows
    live in a dict, and the unresolved-lookup reads it back."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, object]] = {}
        self.notified: list[tuple[str, str]] = []
        self.committed = 0

    def __enter__(self) -> _FakeAlertConn:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def commit(self) -> None:
        self.committed += 1


def _wire_alert_store(monkeypatch: pytest.MonkeyPatch, conn: _FakeAlertConn) -> None:
    """Route `_notify` / `_resolve` through the fake conn + shared.alerts."""
    import shared.alerts as _alerts
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(_probe_mod, "_alert_db_connect", lambda: conn)

    def _lookup(c: object, service: str) -> tuple[str, str] | None:
        for (fp, starts_at), row in conn.rows.items():
            if (
                row.get("status") == "unresolved"
                and row.get("labels", {}).get("service") == service  # type: ignore[union-attr]
            ):
                return starts_at, fp  # type: ignore[return-value]
        return None

    monkeypatch.setattr(_probe_mod, "_unresolved_alert_instance", _lookup)

    def _upsert(
        _c: object, alert: dict[str, object], source: str
    ) -> tuple[object, bool, bool, dict[str, object]]:
        key = (str(alert["fingerprint"]), str(alert["starts_at"]))
        old_row = conn.rows.get(key)
        was_open = old_row is not None and old_row.get("status") == "unresolved"
        # The real store normalizes to unresolved/resolved and keeps notified_at
        # across updates; the fake mirrors both so the unresolved-lookup and the
        # IM gate match the same way.
        conn.rows[key] = {
            **alert,
            "status": "unresolved" if alert["status"] == "firing" else "resolved",
            "notified_at": old_row.get("notified_at") if old_row else None,
        }
        status = alert["status"]
        notified = conn.rows[key].get("notified_at") is not None
        should = (status == "firing" and not notified) or (status == "resolved" and notified)
        return key, not was_open, should, {"notified_at": conn.rows[key].get("notified_at")}

    def _stamp(_c: object, keys: list[object]) -> None:
        for k in keys:
            if k in conn.rows:
                conn.rows[k]["notified_at"] = "yes"  # type: ignore[index]
        conn.notified.extend(keys)  # type: ignore[arg-type]

    monkeypatch.setattr(_alerts, "upsert_alert", _upsert)
    monkeypatch.setattr(_alerts, "stamp_notified", _stamp)
    monkeypatch.setattr(_alerts, "display_language", lambda _c: "en")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _alerts,
        "notify_text",
        lambda _a, _l: f"TEXT:{_a['status']}:{_a['labels']['service']}",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_alerts, "fingerprint", lambda labels: f"fp:{labels['service']}")  # pyright: ignore[reportUnknownArgumentType]


def test_repeated_start_reuses_the_open_alert_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure that is still open when the next start runs is UPDATE d, not
    re-inserted: one alerts row and one IM across N starts (the boot job retries
    every 60 s — this is the anti-spam guarantee)."""
    import shared.alerts as _alerts
    from cli.commands import _probe as _probe_mod

    conn = _FakeAlertConn()
    _wire_alert_store(monkeypatch, conn)
    ims: list[str] = []
    monkeypatch.setattr(_alerts, "notify_im", lambda t: ims.append(t) or True)  # pyright: ignore[reportUnknownArgumentType]

    specs = (_spec("pitr-uploader"),)
    _probe_mod._notify_non_critical_unready_services(specs, im_enabled=True)
    _probe_mod._notify_non_critical_unready_services(specs, im_enabled=True)

    assert len(conn.rows) == 1, "the second start must reuse the open instance"
    assert len(ims) == 1, "only the first firing IM is sent"


def test_recovered_service_resolves_its_open_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that finds the service up again closes the open instance — the
    Inspector's unresolved panel must not keep a fixed failure (user ruling
    2026-08-29)."""
    import shared.alerts as _alerts
    from cli.commands import _probe as _probe_mod

    conn = _FakeAlertConn()
    _wire_alert_store(monkeypatch, conn)
    ims: list[str] = []
    monkeypatch.setattr(_alerts, "notify_im", lambda t: ims.append(t) or True)  # pyright: ignore[reportUnknownArgumentType]

    specs = (_spec("pitr-uploader"),)
    _probe_mod._notify_non_critical_unready_services(specs, im_enabled=True)
    _probe_mod._resolve_recovered_non_critical_alerts(specs, im_enabled=True)

    assert len(conn.rows) == 1, "the resolved edge updates the same instance"
    (key,) = conn.rows
    assert key[0] == "fp:ava-pitr-uploader"
    assert conn.rows[key]["status"] == "resolved"
    assert len(ims) == 2, "one firing IM + one resolved IM"


def test_im_is_suppressed_under_no_readiness_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-readiness-gate` (the boot job's uncapped 60 s retry loop) writes
    the alerts row but sends no IM — the store stays visible, the user's IM
    stays quiet (QA #1196 P1-1)."""
    import shared.alerts as _alerts
    from cli.commands import _probe as _probe_mod

    conn = _FakeAlertConn()
    _wire_alert_store(monkeypatch, conn)
    ims: list[str] = []
    monkeypatch.setattr(_alerts, "notify_im", lambda t: ims.append(t) or True)  # pyright: ignore[reportUnknownArgumentType]

    _probe_mod._notify_non_critical_unready_services((_spec("pitr-uploader"),), im_enabled=False)

    assert len(conn.rows) == 1, "the alerts row must still be written"
    assert ims == [], "the IM push must be suppressed"


# ─── the resolved edge's roster (QA #1196 nit-1 / nit-5) ─────────────────────


def test_recovered_non_critical_specs_excludes_critical_and_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved edge's roster is `started` minus the critical manifest minus
    the failures the wait just returned — so a service that missed its window
    THIS start (even one that came up the instant after) is not resolved by the
    same start that alerted it; it stays open for the next start."""
    from cli.commands import _probe as _probe_mod

    started = (_spec("gateway"), _spec("agent-host"), _spec("pitr-uploader"), _spec("browser"))
    failed = (_spec("pitr-uploader"),)

    recovered = _probe_mod._recovered_non_critical_specs(started, failed)

    assert [s.session for s in recovered] == ["browser"]
