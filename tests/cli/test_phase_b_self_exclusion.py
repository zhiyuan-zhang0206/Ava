"""A rollout must not fire Phase B at the host running it.

The defect (issue #1151, prod rollout 1785568439): a `gateway,agent-runner` single box
is a legitimate `machines.list_agent_runners()` row, so it was included in the Phase-B
fan-out it was itself orchestrating. Its local leg had already checked it out, migrated
it and restarted it, so that leg was pure redundancy — but the op's *first* act is
`ava stop` killing ava-gateway, which opened a ~9 s serving hole immediately AFTER the
readiness gate had passed. Two remote runners' preflight probes landed in the hole,
took ECONNREFUSED, declined with `RESTART_DECLINED_EXIT_CODE`, and were reported
STALLED until the settle lease lapsed ~15 minutes later.

So these pin two independent claims, in two topologies each:

- **which phase drops this host.** Phase 0 (fetch) and Phase A (pause) keep it, because
  their ops are idempotent with the local leg and one code path for single-box and
  split is worth more than the saved dial. Phase B drops it, because its op is not
  idempotent — it kills and re-launches the gateway everyone else depends on.
- **a split deployment is untouched.** A gateway-only unit never carried
  `agent-runner`, so it was never in the list; the exclusion must not shrink a split
  rollout by one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import _update_orchestration as orch
from cli.commands import update as _up
from cli.commands._gateway_ready import GatewayReadiness

# This box's name in every test below — the host that runs the orchestration.
ME = "orchestrator-box"


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """Same seams as tests/cli/test_phase_b_gateway_ready.py: no real git, no real lock,
    no real pin write."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


class _Rollout:
    """One recorded orchestration run: every fan-out path with the hosts it reached."""

    def __init__(self, rc: int, calls: list[tuple[str, list[str]]]) -> None:
        self.rc = rc
        self.calls = calls

    def hosts(self, path: str) -> list[str]:
        """The hosts a given fan-out path was sent to (empty when it never ran)."""
        return [h for call_path, hosts in self.calls if call_path == path for h in hosts]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity,
    *,
    registered: list[tuple[str, str | None]],
    role: str = "gateway,agent-runner",
) -> _Rollout:
    """Run the gateway orchestration with `registered` as the reconciled rollout list
    and this host named `ME`, recording which hosts each phase reached."""
    set_machine_identity(role=role, name=ME)
    calls: list[tuple[str, list[str]]] = []

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        calls.append((path, [h[0] for h in hosts]))  # pyright: ignore[reportUnknownArgumentType]
        return [(name, "ok", "") for name, _url in hosts]

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: list(registered))
    monkeypatch.setattr("shared.machines.list_stopped_agent_runners", list)
    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda hosts, **_unused: {name: _cli.PollVerdict(_cli.POLL_OK) for name, _url in hosts},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    return _Rollout(rc, calls)


# ── the fix: the orchestrator is not its own Phase-B target ────────────────────


def test_single_box_is_not_sent_its_own_self_update(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """The whole defect in one assertion: the co-located gateway,agent-runner host
    orchestrating the rollout must not appear in `/api/cluster/update`, because that
    op would kill the gateway the remote runners are about to probe."""
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None), ("wsl", None)],
    )

    assert rollout.rc == 0
    assert rollout.hosts("/api/cluster/update") == ["air", "wsl"], rollout.calls


def test_the_remote_runners_still_get_phase_b(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """Excluding one host must not be a way to exclude the rollout: every OTHER
    agent-runner is still told to self-update, or the fix would trade a 15-minute
    stall for a fleet that silently never updates."""
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[("air", None), (ME, None), ("wsl", None)],
    )

    assert set(rollout.hosts("/api/cluster/update")) == {"air", "wsl"}


def test_phase_0_and_phase_a_still_include_this_host(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """The exclusion is scoped to the one phase that cannot tolerate the host: Phase 0's
    fetch and Phase A's pause are idempotent with the local leg, and keeping them on one
    code path for single-box and split is the point of listing purely by capability."""
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None)],
    )

    assert ME in rollout.hosts("/api/cluster/fetch")
    assert ME in rollout.hosts("/api/cluster/stop")
    assert ME not in rollout.hosts("/api/cluster/update")


def test_split_deployment_rollout_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """A gateway-only orchestrator never carried `agent-runner`, so it was never in the
    list — every registered runner must still receive Phase B, with no host lost to a
    name that happens not to match."""
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[("air", None), ("wsl", None)],
        role="gateway",
    )

    assert rollout.rc == 0
    assert rollout.hosts("/api/cluster/update") == ["air", "wsl"]


def test_lone_single_box_finishes_clean_with_an_empty_phase_b(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """A box that IS the whole cluster has nobody to fan out to once it drops itself.
    That is a clean rollout, not an incomplete one: the local leg did every piece of
    work Phase B exists to dispatch."""
    rollout = _drive(monkeypatch, set_machine_identity, registered=[(ME, None)])  # pyright: ignore[reportUnknownArgumentType]

    assert rollout.rc == 0
    assert rollout.hosts("/api/cluster/update") == []


def test_lone_single_box_still_has_its_readiness_checked(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """Dropping the last Phase-B target must not drop the readiness question with it.
    The local leg starts the gateway with `--no-readiness-gate` precisely because this
    gate asks it off-box, so a single box whose gateway never rebound has to end
    INCOMPLETE — otherwise a rollout that left the cluster dark reports CLEAN."""
    monkeypatch.setattr(
        _cli,
        "_await_gateway_serving",
        lambda **_kw: (GatewayReadiness.TIMED_OUT, "no answer"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rollout = _drive(monkeypatch, set_machine_identity, registered=[(ME, None)])  # pyright: ignore[reportUnknownArgumentType]

    assert rollout.rc == 1
    assert rollout.hosts("/api/cluster/update") == []


# ── the helper on its own ──────────────────────────────────────────────────────


def test_phase_b_targets_drops_only_this_host(set_machine_identity) -> None:
    """Name equality against `machine_name()` is the whole predicate — ordering and
    every other row are preserved, including the pre-resolved ops URL each carries."""
    set_machine_identity(role="gateway,agent-runner", name=ME)

    assert orch._phase_b_targets(
        [("air", "http://air:8600"), (ME, "http://me:8600"), ("wsl", None)]
    ) == [("air", "http://air:8600"), ("wsl", None)]


def test_phase_b_targets_says_so_when_it_excludes(set_machine_identity, capsys) -> None:
    """A rollout that silently reaches fewer hosts than the count printed a moment
    earlier is how the 2026-07-28 stale-marker exclusion stayed invisible. This one
    announces itself."""
    set_machine_identity(role="gateway,agent-runner", name=ME)

    orch._phase_b_targets([(ME, None), ("air", None)])

    assert ME in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_phase_b_targets_is_silent_when_it_excludes_nothing(set_machine_identity, capsys) -> None:
    """A split rollout must not print an exclusion notice for a host it never had."""
    set_machine_identity(role="gateway", name=ME)

    assert orch._phase_b_targets([("air", None)]) == [("air", None)]
    assert capsys.readouterr().out == ""  # pyright: ignore[reportUnknownMemberType]
