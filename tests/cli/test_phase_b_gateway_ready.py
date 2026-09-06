"""Phase B must not be fired at a gateway that is not serving yet.

The defect these pin: a runner's self-update preflight refuses to stop anything it
cannot un-stop, so a runner told to update before the gateway is reachable declines in
~3 s — and the rollout then *waited* on it as if it were slow. Three prod rollouts on
2026-07-29 stalled that way. So the tests are about **ordering and cost**, in both
directions:

- a not-yet-serving gateway must mean the fan-out never happens (not "happens and is
  survived"), and must reach `RolloutOutcome.INCOMPLETE` — never CLEAN, never ABORTED;
- a serving gateway must cost one probe and no sleep, because a readiness gate that
  taxes every healthy rollout is a bad trade even when it is correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import _gateway_ready as _gr
from cli.commands import update as _up
from cli.commands._gateway_ready import GatewayReadiness
from cli.commands._repo import GatewayProbe
from shared.deploy_timing import GATEWAY_READY_TIMEOUT_S, NO_PROGRESS_TIMEOUT_S

pytestmark = pytest.mark.real_gateway_readiness_gate


# ── the gate itself ────────────────────────────────────────────────────────────


class _Clock:
    """A monotonic clock only `sleep()` advances — so the bound is reached in exactly
    the number of passes the interval implies, in no wall-clock time at all. A real
    clock with `sleep` stubbed out would spin the loop thousands of times per second and
    make the pass count meaningless."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Neutralize the gate's environment: a fixed gateway URL and a fake clock (whose
    `slept` list lets a test assert the healthy path waits for nothing)."""
    clock = _Clock()
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(_gr, "time", clock)
    # Default: the gateway session is alive and nothing answers on loopback — the
    # "still booting" reading, so a test only overrides the seam it is about.
    monkeypatch.setattr(_gr, "_gateway_session_alive", lambda: True)
    monkeypatch.setattr(_gr, "_serving_locally", lambda: False)
    # Default: the responder is this unit's gateway (the ownership check the
    # SERVING branch now applies — Task #965); a test about the check overrides.
    monkeypatch.setattr(_gr, "_gateway_listener_owned", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]
    return clock


def _answers(monkeypatch: pytest.MonkeyPatch, *sequence: GatewayProbe) -> list[str]:
    """Script `probe_gateway_once`'s answers; the last one repeats forever. Returns the
    list of URLs dialed, so a test can count probes."""
    dialed: list[str] = []
    answers = list(sequence)

    def _probe(url: str, *, timeout_s: float = 10.0) -> GatewayProbe:
        dialed.append(url)
        return answers[min(len(dialed) - 1, len(answers) - 1)]

    monkeypatch.setattr(_gr, "probe_gateway_once", _probe)
    return dialed


def test_serving_gateway_costs_one_probe_and_no_wait(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """The healthy path is the common path: a gateway already serving is one dial and
    zero sleep. A fix that adds seconds to every good rollout is the wrong fix."""
    dialed = _answers(monkeypatch, GatewayProbe(200, "{}"))

    verdict, _detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.SERVING
    assert len(dialed) == 1, dialed
    assert probes.slept == [], "a serving gateway must not be slept on"


def test_gateway_that_binds_late_is_waited_for(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """Connection-refused is the *expected* reading mid-boot (the prod log's
    `[Errno 61]`), not a verdict: the gate keeps dialing and reports SERVING when the
    port finally binds."""
    dialed = _answers(
        monkeypatch,
        GatewayProbe(None, "[Errno 61] Connection refused"),
        GatewayProbe(None, "[Errno 61] Connection refused"),
        GatewayProbe(200, "{}"),
    )

    verdict, _detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.SERVING
    assert len(dialed) == 3, dialed


def test_5xx_is_not_a_refusal(monkeypatch: pytest.MonkeyPatch, probes: _Clock) -> None:
    """A 5xx is "up but not ready" and must keep the gate waiting. Reading it as
    terminal would abandon a gateway one second from serving."""
    dialed = _answers(monkeypatch, GatewayProbe(503, "starting"), GatewayProbe(200, "{}"))

    verdict, _detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.SERVING
    assert len(dialed) == 2, dialed


def test_non_200_answer_refuses_immediately(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """An authenticated 401/403/404 is direct evidence, not a timing problem: waiting
    cannot fix a credential or route mismatch, so the gate must not spend the bound."""
    dialed = _answers(monkeypatch, GatewayProbe(401, "unauthorized"))

    verdict, detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.REFUSED
    assert len(dialed) == 1, dialed
    assert "401" in detail


def test_dead_gateway_session_exits_before_the_bound(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """A gateway whose session died will never bind — the port cannot appear from a
    process that exited. Two confirmations, then out; the bound is untouched."""
    dialed = _answers(monkeypatch, GatewayProbe(None, "[Errno 61] Connection refused"))
    monkeypatch.setattr(_gr, "_gateway_session_alive", lambda: False)

    verdict, detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.GATEWAY_GONE
    assert len(dialed) == _gr._EARLY_EXIT_CONFIRMATIONS, dialed
    assert "session is not running" in detail


def test_off_box_blackhole_is_attributed_not_waited_out(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """Serving on loopback but not at the address the cluster dials: the process is
    healthy and the path to it is not (the app-firewall rule a `uv python` bump
    detaches, issue #949). Waiting cannot fix a firewall rule, and the report must say
    which of the two broke."""
    dialed = _answers(monkeypatch, GatewayProbe(None, "timed out"))
    monkeypatch.setattr(_gr, "_serving_locally", lambda: True)

    verdict, detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.OFF_BOX_UNREACHABLE
    assert len(dialed) == _gr._EARLY_EXIT_CONFIRMATIONS, dialed
    assert "loopback" in detail


def test_one_contrary_reading_is_not_evidence(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """A single "session gone" reading between two refused dials is a race with the
    session lookup, not proof — the gate needs two in a row, and a gateway that then
    serves must still be reported SERVING."""
    dialed = _answers(
        monkeypatch,
        GatewayProbe(None, "refused"),
        GatewayProbe(None, "refused"),
        GatewayProbe(200, "{}"),
    )
    alive = iter([True, False, True, True])
    monkeypatch.setattr(_gr, "_gateway_session_alive", lambda: next(alive))

    verdict, _detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.SERVING
    assert len(dialed) == 3, dialed


def test_bound_expires_into_timed_out(monkeypatch: pytest.MonkeyPatch, probes: _Clock) -> None:
    """A gateway alive but binding nothing is the one case that spends the bound. It
    must END — a readiness wait that can hang is worse than the fast decline it
    replaced — and it must expire as TIMED_OUT, distinct from the three diagnosable
    failures."""
    dialed = _answers(monkeypatch, GatewayProbe(None, "[Errno 61] Connection refused"))

    verdict, detail = _gr.await_gateway_serving(timeout_s=3.0)

    assert verdict is GatewayReadiness.TIMED_OUT
    assert "within 3s" in detail
    # It dialed repeatedly rather than returning on the first pass, and it stopped.
    assert len(dialed) > 1, dialed


def test_unset_gateway_url_is_terminal(monkeypatch: pytest.MonkeyPatch, probes: _Clock) -> None:
    """No configured gateway address means no runner can reach this gateway either.
    Terminal, and reported as a refusal rather than as a timeout — nothing is pending."""
    from shared.machine import GatewayApiBaseMissing

    def _raise() -> str:
        raise GatewayApiBaseMissing("gateway_url unset")

    monkeypatch.setattr("shared.machine.gateway_api_base", _raise)
    _answers(monkeypatch, GatewayProbe(200, "{}"))

    verdict, _detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.REFUSED


def test_every_non_serving_verdict_has_its_own_guidance() -> None:
    """Four verdicts exist because the operator's next move differs; a table that
    collapsed two of them back into one sentence would undo the point (the same reason
    `degraded` was split into converging/stalled)."""
    lines = {
        v: _gr.gateway_readiness_detail(v, "detail")
        for v in GatewayReadiness
        if v is not GatewayReadiness.SERVING
    }
    assert len(set(lines.values())) == len(lines), lines


# ── the gate's place in the orchestration ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """Same seams as tests/cli/test_update_autounpause.py: no real git, no real lock."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def _drive_rollout(
    monkeypatch: pytest.MonkeyPatch,
    *,
    readiness: tuple[GatewayReadiness, str],
    phase_a: str = "ok",
) -> tuple[int, list[tuple[str, list[str]]], list[str]]:
    """Run the gateway orchestration with two agent-runners, a succeeding local update
    and a scripted readiness verdict. Returns (rc, fan-out calls, settle-hold hosts)."""
    calls: list[tuple[str, list[str]]] = []
    settled: list[str] = []

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        calls.append((path, [h[0] for h in hosts]))  # pyright: ignore[reportUnknownArgumentType]
        status = phase_a if path == "/api/cluster/stop" else "ok"
        return [(name, status, "") for name, _url in hosts]

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None), ("b", None)])
    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_await_gateway_serving", lambda **_kw: readiness)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda hosts, **_unused: {name: _cli.PollVerdict(_cli.POLL_OK) for name, _url in hosts},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "settle_update_lock", lambda _holder, hosts: settled.extend(hosts))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    return rc, calls, settled


def test_not_serving_gateway_means_no_fan_out_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point. A gateway that is not serving must not be handed a list of
    runners to make depend on it: `/api/cluster/update` is never sent, so no host
    declines and no host is then waited on. rc=1."""
    rc, calls, _settled = _drive_rollout(
        monkeypatch, readiness=(GatewayReadiness.TIMED_OUT, "no answer")
    )

    assert rc == 1
    assert [c for c in calls if c[0] == "/api/cluster/update"] == [], calls
    # ...and the hosts Phase A paused are resumed rather than stranded.
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [
        ("/api/cluster/resume", ["a", "b"])
    ], calls


def test_expired_readiness_is_incomplete_not_aborted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An expired readiness wait is INCOMPLETE: the gateway migrated and advanced the
    pin, so re-running `ava cluster update` into it is the 2026-07-29 collision. Reporting
    ABORTED would print exactly that instruction — and CLEAN is the collapse PR #937
    removed."""
    rc, _calls, _settled = _drive_rollout(
        monkeypatch, readiness=(GatewayReadiness.TIMED_OUT, "no answer")
    )
    out = capsys.readouterr()

    assert rc == 1
    assert "ROLLOUT INCOMPLETE" in out.err
    assert "ROLLOUT ABORTED" not in out.err
    assert "do NOT re-run `ava cluster update` yet" in out.err


def test_readiness_failure_holds_the_lease_over_the_paused_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cluster IS mid-transition — every runner now runs old code against a
    migrated schema — so the lease converts to a settle hold instead of being released,
    over exactly the hosts Phase A paused."""
    _rc, _calls, settled = _drive_rollout(
        monkeypatch, readiness=(GatewayReadiness.GATEWAY_GONE, "session gone")
    )

    assert sorted(settled) == ["a", "b"]


def test_readiness_failure_holds_only_hosts_that_acked_phase_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acked-only survives in the form PR #937 gave it: a host that never took the
    pause is not running anything this rollout started, so a powered-off runner must
    not make every rollout idle out a settle window."""
    _rc, _calls, settled = _drive_rollout(
        monkeypatch,
        readiness=(GatewayReadiness.TIMED_OUT, "no answer"),
        phase_a="unreachable",
    )

    assert settled == []


def test_ready_gateway_proceeds_to_phase_b_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is a precondition, not a step that can be skipped or reordered: it runs
    after the local update and before the fan-out, and a SERVING verdict costs the
    rollout nothing."""
    order: list[str] = []

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        order.append(path)  # pyright: ignore[reportUnknownArgumentType]
        return [(name, "ok", "") for name, _url in hosts]

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **_kw: order.append("local-update") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_await_gateway_serving",
        lambda **_kw: order.append("readiness") or (GatewayReadiness.SERVING, "http://gw"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda hosts, **_unused: {name: _cli.PollVerdict(_cli.POLL_OK) for name, _url in hosts},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")

    assert rc == 0
    assert order == [
        "/api/cluster/fetch",
        "/api/cluster/stop",
        "local-update",
        "readiness",
        "/api/cluster/update",
    ], order


def test_single_host_cluster_skips_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No agent-runners means no Phase B, so there is nothing to gate: a cluster with
    no dependents must not be able to fail a rollout on its own readiness."""
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_await_gateway_serving",
        lambda **_kw: pytest.fail("no Phase B, no gate"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin") == 0


# ── the gate and the preflight cannot drift apart ──────────────────────────────


def test_gate_and_runner_preflight_share_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that probed a *different* endpoint than the runner's preflight would be
    the "usually true" fix: green here, refused there. Both must go through
    `probe_gateway_once`, so the gate's success criterion IS the preflight's."""
    seen: list[str] = []

    def _probe(url: str, *, timeout_s: float = 10.0) -> GatewayProbe:
        seen.append(url)
        return GatewayProbe(200, "{}")

    monkeypatch.setattr("cli.commands._repo.probe_gateway_once", _probe)
    monkeypatch.setattr(_gr, "probe_gateway_once", _probe)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    # the ownership check is a separate concern — pin it owned for this test
    monkeypatch.setattr(_gr, "_gateway_listener_owned", lambda _url: True)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli._probe_gateway_or_die("http://gw:8000") == 0
    assert _gr.await_gateway_serving()[0] is GatewayReadiness.SERVING
    assert seen == ["http://gw:8000", "http://gw:8000"]


def test_probe_path_is_exempt_from_the_paused_middleware() -> None:
    """The gate waits for *serving*, not for *unpaused*. That only works because the
    probe path is one the cluster-paused 503 middleware bypasses — if it moved to a
    gated path, the gate would wait out its bound on every rollout, since Phase A
    paused this host on purpose."""
    from cli.commands._repo import GATEWAY_PROBE_PATH

    assert GATEWAY_PROBE_PATH.startswith("/api/cluster/")


def test_readiness_bound_sits_under_the_no_progress_family() -> None:
    """`GATEWAY_READY_TIMEOUT_S` answers a different question from
    `NO_PROGRESS_TIMEOUT_S` (one local port bind vs a remote checkout+sync+restart) and
    must stay well under it — and far under the lease TTL, because this wait runs
    *before* the poll that arms lease renewal, so it spends lease time unrenewed."""
    from shared.cluster_lock import LOCK_TTL_S

    assert GATEWAY_READY_TIMEOUT_S < NO_PROGRESS_TIMEOUT_S
    assert GATEWAY_READY_TIMEOUT_S * 5 < LOCK_TTL_S


# --- the OFF_BOX_UNREACHABLE verdict names its remedy -----------------------
#
# The verdict has two plausible causes (a stale macOS ALF rule, or an address the
# runners cannot dial) and the operator-facing sentence used to name both without
# choosing. It now asks the same unprivileged audit `ava converge` reports from,
# so the rollout report and the converge report cannot contradict each other.
#
# These stub the audit. No test here — and none in the suite — mutates a real
# firewall; the privileged repair is only ever printed for a human to run.


def test_off_box_detail_names_the_firewall_repair_when_the_audit_finds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ALF rule really is missing, the rollout report carries the fix.

    An operator reading a failed rollout should not have to go find the runbook: the
    two `sudo` commands and the follow-up restart are the whole remedy.
    """
    import cli.commands._converge_firewall as cfw
    from shared.macos_firewall import FirewallAudit, FirewallVerdict

    missing = Path("/uv/cpython-3.12.11/bin/python3.12")
    monkeypatch.setattr(
        cfw,
        "audit_this_host",
        lambda _roles: FirewallAudit(  # pyright: ignore[reportUnknownArgumentType]
            FirewallVerdict.RULES_MISSING, "1 of 1 have no ALF allow rule", missing=(missing,)
        ),
    )

    text = _gr.gateway_readiness_detail(GatewayReadiness.OFF_BOX_UNREACHABLE, "serving on loopback")

    assert "firewall IS the cause" in text
    assert "--add" in text and str(missing) in text


def test_off_box_detail_rules_the_firewall_out_when_it_is_innocent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux gateway, or one with ALF off, must be sent to the address instead.

    This is the half a static sentence could not do: naming the firewall every time
    trains an operator to check it first even on hosts where it filters nothing, and
    the real cause (AVA_GATEWAY_URL / AVA_MACHINE_HOST) goes unexamined.
    """
    import cli.commands._converge_firewall as cfw
    from shared.macos_firewall import FirewallAudit, FirewallVerdict

    monkeypatch.setattr(
        cfw,
        "audit_this_host",
        lambda _roles: FirewallAudit(FirewallVerdict.FIREWALL_OFF, "the firewall is off"),  # pyright: ignore[reportUnknownArgumentType]
    )

    text = _gr.gateway_readiness_detail(GatewayReadiness.OFF_BOX_UNREACHABLE, "serving on loopback")

    assert "firewall is NOT the cause" in text
    assert "AVA_GATEWAY_URL" in text
    assert "--add" not in text


def test_a_broken_audit_still_yields_a_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller is already explaining a failure; an exception here would replace a
    real diagnosis with a stack trace and lose the verdict entirely."""
    import cli.commands._converge_firewall as cfw

    def boom(roles: object) -> object:
        raise RuntimeError("socketfilterfw vanished")

    monkeypatch.setattr(cfw, "audit_this_host", boom)

    text = _gr.gateway_readiness_detail(GatewayReadiness.OFF_BOX_UNREACHABLE, "serving on loopback")

    assert "could not audit the host firewall" in text
    assert "socketfilterfw vanished" in text
    assert "#949" in text


def test_serving_refused_when_port_held_by_foreign_listener(
    monkeypatch: pytest.MonkeyPatch, probes: _Clock
) -> None:
    """A 200 from the probe endpoint is not enough: if the process answering
    the gateway port is NOT this unit's gateway (a foreign listener — another
    cluster's gateway, a leftover daemon, a test server), the gate refuses
    instead of blessing a Phase B against the wrong responder. This is the
    verify-leg half of the orphan closure (Task #965): the old process holding
    the port must never read as the new code serving."""
    dialed = _answers(monkeypatch, GatewayProbe(200, "{}"))
    monkeypatch.setattr(_gr, "_gateway_listener_owned", lambda _url: False)  # pyright: ignore[reportUnknownArgumentType]

    verdict, detail = _gr.await_gateway_serving()

    assert verdict is GatewayReadiness.REFUSED
    assert "foreign listener" in detail
    assert len(dialed) == 1  # the ownership check adds no dials


def test_gateway_listener_owned_applies_unit_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ownership check resolves the listener pid on the dialed port and
    applies the unit predicate: ours -> True, positively foreign -> False, and
    an inconclusive scan (no listener readable) is not treated as foreign —
    the probe is the primary evidence."""
    calls: dict[str, object] = {}

    def _fake_listeners(port: int) -> list[int]:
        calls["port"] = port
        return calls["pids"]  # type: ignore[no-any-return]

    def _fake_mentions(pid: int, markers: tuple[str, ...]) -> bool:
        calls["pid"] = pid
        return pid in calls["ours"]  # type: ignore[operator]

    monkeypatch.setattr(_gr, "_repo_root", lambda: Path("/repo/x"))
    monkeypatch.setattr(_gr, "ava_home", lambda: Path("/home/x"))
    monkeypatch.setattr(_gr, "listeners_on", _fake_listeners)
    monkeypatch.setattr(_gr, "process_mentions", _fake_mentions)

    calls["pids"] = [101]
    calls["ours"] = {101}
    assert _gr._gateway_listener_owned("http://gw:8000") is True

    calls["pids"] = [101]
    calls["ours"] = set()
    assert _gr._gateway_listener_owned("http://gw:8000") is False

    calls["pids"] = []
    assert _gr._gateway_listener_owned("http://gw:8000") is True  # inconclusive
    assert calls["port"] == 8000
