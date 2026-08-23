"""A live session is not a running service — the husk path (issue #1015).

Two mechanisms composed into a silent skip on 2026-07-30: `ava cluster update`'s stop
reported `⚠ ava-gateway (forced)` for a kill it never confirmed, and the start that
followed printed `✓ ava-gateway already running` for the session that survived it.
Prod ran without a gateway for a minute; the watchdog, not the start, brought it
back. Neither half printed anything an operator would read as a failure.

What is asserted here:

- the force branch of a kill answers from `has-session`, not from `kill-session`'s
  exit status, so a session that outlives the kill comes back as a failure;
- `ava start`'s idempotence guard asks the service's probe, so a session with a dead
  daemon behind it is relaunched instead of skipped;
- a service the probe cannot judge that early (the frontend, mid-`npm run build`) is
  still skipped, because "slow to serve" and "husk" must not be the same answer;
- a husk that cannot be cleared is reported as a not-relaunched service rather
  than being handed to the session backend, which would only fail on the
  duplicate name.

Nothing here starts a real session: the backend's process records / kill paths
and the cli-layer probe seams are stubbed, and what is checked is which commands
the code decided to run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import cli.commands as _cli
from cli.commands._probe import ServiceProbe
from cli.commands._repo import ServiceSpec
from ops.spec import _GATEWAY


def _spec(service: str) -> ServiceSpec:
    return ServiceSpec(
        session=service,
        cmd=f".venv/bin/python -m {service}",
        capabilities=_GATEWAY,
        requires_db=True,
        curl_url="http://localhost:1/healthz",
    )


# The confirm window's throttle and bound come from the suite-wide
# `_guard_force_kill_confirmation` fixture; the tests below that care about a
# specific bound set `_FORCE_CONFIRM_TIMEOUT_S` themselves on top of it. Both are
# module-local names bound for exactly this, so patching either cannot reach any
# other wall-clock loop in the process (the mistake that made a test allocate 26 GB
# in issue #1001).


# ── mechanism 1: the kill confirms, or says it did not ──────────────────────


class _FakeProc:
    """The psutil.Process-shaped stub `_process_for_record` hands the kill path."""

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self.killed = False
        self.pid = 4242

    def is_running(self) -> bool:
        return self._alive

    def children(self, recursive: bool = False) -> list[object]:
        return []

    def terminate(self) -> None:
        self.killed = True

    def kill(self) -> None:
        self.killed = True


def _use_record(
    monkeypatch: pytest.MonkeyPatch,
    proc: _FakeProc,
    *,
    record: bool = True,
) -> dict[str, list[str]]:
    """Point `posixproc.kill_session` at an in-memory record + process."""
    import shared.posixproc as pp
    from shared.session_record import SessionRecord

    if record:
        monkeypatch.setattr(
            pp,
            "_read_record",
            lambda _name: SessionRecord(  # pyright: ignore[reportUnknownArgumentType]
                pid=4242, create_time=1.0, cmd="x", cwd="/", started_at=1.0
            ),
        )
    else:
        monkeypatch.setattr(pp, "_read_record", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pp, "_process_for_record", lambda _rec: proc)  # pyright: ignore[reportUnknownArgumentType]
    unlinked: list[str] = []

    class _P:
        def unlink(self, **_: object) -> None:
            unlinked.append("unlinked")

    monkeypatch.setattr(pp, "_record_path", lambda _name: _P())  # pyright: ignore[reportUnknownArgumentType]
    return {"unlinked": unlinked}


def test_force_kill_reports_failure_when_the_process_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill that the process survives is a FAILED kill. The supervisor's answer
    comes from re-asking the process, not from the kill call itself — before the
    #1015 fix the force branch returned (True, 'forced') unconditionally, which
    is the `⚠ ava-gateway (forced)` prod read as a successful stop."""
    from shared import posixproc as pp

    proc = _FakeProc(alive=True)  # survives everything
    _use_record(monkeypatch, proc)

    ok, mode = pp.kill_session("ava-gateway", graceful=False)

    assert ok is False, "a process still answering is_running was not killed"
    assert mode == "forced"


def test_force_kill_confirms_death_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process is gone once the kill lands → (True, 'forced'), and the stale
    record is cleaned up so the next `has_session` reads dead."""
    from shared import posixproc as pp

    proc = _FakeProc(alive=True)
    state = _use_record(monkeypatch, proc)

    def _kill_tree(_proc, *, graceful: bool, timeout: float) -> None:
        _proc._alive = False  # the tree teardown actually worked

    monkeypatch.setattr(pp, "_terminate_tree", _kill_tree)  # pyright: ignore[reportUnknownArgumentType]

    assert pp.kill_session("ava-gateway", graceful=False) == (True, "forced")
    assert state["unlinked"] == ["unlinked"], "the dead record must be removed"


def test_force_kill_of_an_absent_session_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with no record is a noop — the backend's documented contract is
    idempotence, and the answer comes from the record read, not from any kill."""
    from shared import posixproc as pp

    _use_record(monkeypatch, _FakeProc(alive=True), record=False)

    assert pp.kill_session("ava-nope", graceful=False) == (True, "noop")


def test_graceful_kill_timeout_delegates_to_the_confirming_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful path hands `_terminate_tree` the wait-then-escalate policy
    (SIGTERM, wait up to the timeout, SIGKILL the survivors) and then answers
    from the same post-kill confirmation — a graceful stop that fails is reported
    as a failure, not as a success. `mode` says what happened too: the signal did
    not end this tree, so it reads `forced` however the caller asked for it."""
    from shared import posixproc as pp

    proc = _FakeProc(alive=True)
    _use_record(monkeypatch, proc)
    calls: list[dict[str, object]] = []

    def _kill_tree(_proc, *, graceful: bool, timeout: float) -> bool:
        calls.append({"graceful": graceful, "timeout": timeout})
        return False  # the tree outlived the graceful signal

    monkeypatch.setattr(pp, "_terminate_tree", _kill_tree)  # pyright: ignore[reportUnknownArgumentType]

    ok, mode = pp.kill_session("ava-gateway", graceful=True, timeout=0.01)

    assert calls == [{"graceful": True, "timeout": 0.01}]
    assert (ok, mode) == (False, "forced"), "a surviving process must not read as stopped"


def test_graceful_kill_reports_graceful_when_the_signal_ended_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the mode mapping: a tree that ended on SIGTERM alone
    reads `graceful`, so the caller's clean-stop marker means something. Both
    halves are pinned here because `mode` used to echo the requested flag —
    every force-killed service reported a clean stop, which is how a graceful
    stop that never reached any daemon stayed invisible."""
    from shared import posixproc as pp

    proc = _FakeProc(alive=False)
    _use_record(monkeypatch, proc)

    def _kill_tree(_proc, *, graceful: bool, timeout: float) -> bool:
        del graceful, timeout
        return True  # SIGTERM alone ended it

    monkeypatch.setattr(pp, "_terminate_tree", _kill_tree)  # pyright: ignore[reportUnknownArgumentType]

    assert pp.kill_session("ava-gateway", graceful=True, timeout=15.0) == (True, "graceful")


# ── mechanism 2: the start's guard asks the service, not the session ────────


def _roster(monkeypatch: pytest.MonkeyPatch, *services: str) -> None:
    """Pin the roster `_launch_sessions` iterates. It binds
    `_services_for_roles_annotated` at import time, so the patch goes on the
    sub-module, not the package namespace."""
    from cli.commands import _session_lifecycle as _session_mod

    annotated = tuple((_spec(s), None) for s in services)
    monkeypatch.setattr(_session_mod, "_services_for_roles_annotated", lambda _r: annotated)  # pyright: ignore[reportUnknownArgumentType]


def _launch_probe_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions_alive: set[str],
    probe: ServiceProbe,
) -> dict[str, list[str]]:
    """Stub the three seams `_launch_sessions` reaches through and record them."""
    seen: dict[str, list[str]] = {"killed": [], "launched": []}
    monkeypatch.setattr(_cli, "_has_session", lambda s: s in sessions_alive)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: probe)  # pyright: ignore[reportUnknownArgumentType]

    def _kill(session: str, **_kw: object) -> bool:
        seen["killed"].append(session)
        return True

    def _new(session: str, _cmd: str, _cwd: Path, **_kw: object) -> bool:
        seen["launched"].append(session)
        return True

    monkeypatch.setattr(_cli, "_kill_session", _kill)
    monkeypatch.setattr(_cli, "_new_session", _new)
    return seen


def _stub_session_code_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    launched: str,
    head: str,
    lease_kind: Literal["free", "unreadable", "executing", "settle_hold"] = "free",
    orchestration_kind: Literal["none", "in_flight", "unreadable"] = "none",
) -> None:
    """Make the session-code check inspect known local deployment state."""
    from ops.controllers import _deploy_state
    from shared import cluster_drift, session_code

    monkeypatch.setattr(cluster_drift, "running_from_prod_source", lambda: True)
    monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: head)

    def _launched_sha(_session: str) -> str:
        return launched

    def _lease_state(*, settle_hold_mode: Literal["narrow", "pass"]) -> _deploy_state.LeaseVerdict:
        del settle_hold_mode
        return _deploy_state.LeaseVerdict(lease_kind, "updater", None)

    monkeypatch.setattr(session_code, "launched_sha", _launched_sha)
    monkeypatch.setattr(
        _deploy_state,
        "read_lease_state",
        _lease_state,
    )
    monkeypatch.setattr(
        _deploy_state,
        "read_orchestration",
        lambda: _deploy_state.OrchestrationState(orchestration_kind, None),
    )


def test_husk_session_is_cleared_and_relaunched(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The incident, replayed: `ava-gateway`'s session exists, nothing is behind it.
    Before the fix this printed `✓ ava-gateway already running` and launched nothing."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-gateway"},
        probe=ServiceProbe(False, "http", "no 2xx/3xx from http://localhost:8000/api/agents"),
    )
    _roster(monkeypatch, "gateway")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen["killed"] == ["ava-gateway"], "the husk must be cleared before relaunching"
    assert seen["launched"] == ["ava-gateway"]
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "already running" not in out
    assert "no 2xx/3xx" in out, "the operator is told which probe said the service is down"


def test_live_service_is_still_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A healthy session already launched on the checkout's code is left alone."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-gateway"},
        probe=ServiceProbe(True, "identity", ""),
    )
    _stub_session_code_state(monkeypatch, launched="current", head="current")
    _roster(monkeypatch, "gateway")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen == {"killed": [], "launched": []}
    assert "✓ ava-gateway already running" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_live_service_on_stale_code_is_cleared_and_relaunched(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A healthy session carrying a prior checkout is not an idempotent start."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-gateway"},
        probe=ServiceProbe(True, "identity", ""),
    )
    _stub_session_code_state(monkeypatch, launched="oldsha", head="newsha")
    _roster(monkeypatch, "gateway")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen["killed"] == ["ava-gateway"]
    assert seen["launched"] == ["ava-gateway"]
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "stale code" in out


@pytest.mark.parametrize("guard", ("update", "local-orchestration", "dev-worktree"))
def test_stale_code_session_is_left_alone_while_its_check_is_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guard: str
) -> None:
    """Updates own code transitions, and dev/prod checkout pairs are incomparable."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-gateway"},
        probe=ServiceProbe(True, "identity", ""),
    )
    if guard == "update":
        _stub_session_code_state(
            monkeypatch,
            launched="oldsha",
            head="newsha",
            lease_kind="executing",
        )
    elif guard == "local-orchestration":
        _stub_session_code_state(
            monkeypatch,
            launched="oldsha",
            head="newsha",
            orchestration_kind="in_flight",
        )
    else:
        from shared import cluster_drift

        monkeypatch.setattr(cluster_drift, "running_from_prod_source", lambda: False)
    _roster(monkeypatch, "gateway")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen == {"killed": [], "launched": []}


def test_probeless_service_keeps_the_session_only_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`alive is None` (browser-mcp: an AF_UNIX transport only its own healthcheck
    dials) is "never observed", not "down". There is no evidence to overrule the
    session with, so the old guard stands."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-browser-mcp"},
        probe=ServiceProbe(None, "n/a", ""),
    )
    _roster(monkeypatch, "browser-mcp")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen == {"killed": [], "launched": []}


def test_frontend_mid_build_is_not_a_husk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The frontend answers its healthcheck ~30-60 s after its pane exists, so a
    False probe there means "still building", not "dead". Relaunching would restart
    the build the session is in the middle of — and would do it on every retry."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-frontend"},
        probe=ServiceProbe(False, "http", "no 2xx/3xx from http://localhost:3000/"),
    )
    _roster(monkeypatch, "frontend")

    _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen == {"killed": [], "launched": []}


def test_uncleanable_husk_is_reported_not_relaunched(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """When the husk survives the clearing kill too, a fresh spawn would only
    fail on the duplicate name. Say so instead, and leave the spec on the returned
    roster so the readiness gate turns it into the start's exit code."""
    seen = _launch_probe_env(
        monkeypatch,
        sessions_alive={"ava-gateway"},
        probe=ServiceProbe(False, "http", "no 2xx/3xx"),
    )
    monkeypatch.setattr(_cli, "_kill_session", lambda s, **_kw: seen["killed"].append(s) or False)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, "gateway")

    launch = _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert seen["launched"] == []
    assert "could not clear the stale session" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert [s.session for s in launch.started] == ["gateway"], (
        "the readiness gate must still see this service, so the start exits non-zero"
    )
    assert launch.failed == (), (
        "an uncleanable husk is not a launch failure: the session is still there and "
        "the husk verdict came from one probe, so the readiness gate must judge it"
    )


def test_husk_reason_matches_the_readiness_gate_exemption(monkeypatch: pytest.MonkeyPatch) -> None:
    """The husk check and the readiness wait exempt the same services for the same
    reason, from one predicate — two copies of the frontend rule would drift."""
    from cli.commands import _probe as _probe_mod

    monkeypatch.setattr(_cli, "_probe_service", lambda _s: ServiceProbe(False, "http", "down"))  # pyright: ignore[reportUnknownArgumentType]

    assert _probe_mod._probe_judges_a_fresh_launch(_spec("gateway")) is True
    assert _probe_mod._probe_judges_a_fresh_launch(_spec("frontend")) is False
    assert _probe_mod._husk_session_reason(_spec("gateway")) == "down"
    assert _probe_mod._husk_session_reason(_spec("frontend")) is None
