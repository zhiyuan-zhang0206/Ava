"""An unreachable runner cannot acknowledge the drain required before migration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import update as _up

# This box's name in every test below — the host that runs the orchestration.
ME = "orchestrator-box"


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """Same seams as tests/cli/test_phase_b_self_exclusion.py: no real git, no real
    lock, no real pin write."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


class _Rollout:
    """One recorded orchestration run: rc + every fan-out path with the (host,
    verdict) pairs it dialed."""

    def __init__(self, rc: int, calls: list[tuple[str, list[tuple[str, str]]]]) -> None:
        self.rc = rc
        self.calls = calls

    def results(self, path: str) -> list[tuple[str, str]]:
        """The (host, verdict) pairs a fan-out path dialed (empty when it never ran)."""
        return [pair for call_path, pairs in self.calls if call_path == path for pair in pairs]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Callable[..., None],
    *,
    registered: list[tuple[str, str | None]],
    statuses: dict[str, dict[str, str]] | None = None,
    local_update_rc: int = 0,
) -> _Rollout:
    """Run the gateway orchestration with `registered` as the reconciled rollout
    list and this host named `ME`, recording which hosts each phase reached.

    `statuses` maps fan-out path -> host -> verdict ("ok" / "unreachable" /
    "fatal") for that phase's dial; a host with no entry answers "ok". An unreachable host aborts before any later phase.
    """
    set_machine_identity(role="gateway,agent-runner", name=ME)
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def _fan_out(
        hosts: list[tuple[str, str | None]],
        path: str,
        _timeout: float,
        payload: object | None = None,
    ) -> list[tuple[str, str, str]]:
        detail = {"unreachable": "ops server unreachable", "fatal": "git fetch failed"}
        out: list[tuple[str, str, str]] = []
        for name, _url in hosts:
            verdict = (statuses or {}).get(path, {}).get(name, "ok")
            out.append((name, verdict, detail.get(verdict, "")))
        calls.append((path, [(name, verdict) for name, verdict, _ in out]))
        return out

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: list(registered))
    monkeypatch.setattr("shared.machines.list_stopped_agent_runners", list)
    monkeypatch.setattr(_cli, "_fan_out", _fan_out)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: local_update_rc)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda hosts, **_unused: {name: _cli.PollVerdict(_cli.POLL_OK) for name, _url in hosts},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    return _Rollout(rc, calls)


@pytest.mark.parametrize(
    "unreachable", [{"air": "unreachable"}, {"air": "unreachable", "wsl": "unreachable"}]
)
def test_unreachable_registered_runner_aborts_before_pause_or_migration(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    unreachable: dict[str, str],
) -> None:
    rollout = _drive(
        monkeypatch,
        set_machine_identity,
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses={"/api/cluster/fetch": unreachable},
    )
    assert rollout.rc == 1
    assert rollout.results("/api/cluster/stop") == []
    assert rollout.results("/api/cluster/update") == []
    assert rollout.results("/api/cluster/resume") == []
    assert "skipping" not in capsys.readouterr().err


def test_failed_fetch_aborts_before_pause(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity: Callable[..., None]
) -> None:
    rollout = _drive(
        monkeypatch,
        set_machine_identity,
        registered=[(ME, None), ("wsl", None)],
        statuses={"/api/cluster/fetch": {"wsl": "fatal"}},
    )
    assert rollout.rc == 1
    assert rollout.results("/api/cluster/stop") == []
    assert rollout.results("/api/cluster/update") == []


def test_phase_a_partial_failure_resumes_every_participant(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity: Callable[..., None]
) -> None:
    rollout = _drive(
        monkeypatch,
        set_machine_identity,
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses={"/api/cluster/stop": {"air": "unreachable"}},
    )
    assert rollout.rc == 1
    assert rollout.results("/api/cluster/update") == []
    assert [name for name, _ in rollout.results("/api/cluster/resume")] == [ME, "air", "wsl"]


@pytest.mark.parametrize("results", [[], [("a", "ok", "")]])
def test_missing_fetch_or_drain_acknowledgement_cannot_pass(
    monkeypatch: pytest.MonkeyPatch, results: list[tuple[str, str, str]]
) -> None:
    from cli.commands._update_pause import _run_phase_a
    from cli.commands._update_preflight import _run_preflight_fetch

    monkeypatch.setattr(_cli, "_fan_out", lambda *_a, **_kw: results)  # pyright: ignore[reportUnknownArgumentType]
    runners: list[tuple[str, str | None]] = [("a", None), ("b", None)]
    assert _run_preflight_fetch(runners, restart_only=False)
    assert _run_phase_a(runners, deploy_capability={}) is None
