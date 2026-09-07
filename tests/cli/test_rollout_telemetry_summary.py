"""The gateway orchestration ends its log with the telemetry summary (Task #1820).

The brief's 368s breakdown was reconstructed by hand afterwards — the per-phase
numbers existed only scattered through the log text. These pin the aggregate:
every phase of a driven orchestration records into the one `[rollout-telemetry]`
JSON line, on the success path and on the aborts (which are exactly the runs an
operator needs the numbers for).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import update as _up

ME = "orchestrator-box"


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """Same seams as tests/cli/test_phase_b_self_exclusion.py: no real git, no
    real lock, no real pin write."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity,
    *,
    registered: list[tuple[str, str | None]],
) -> None:
    """Run the gateway orchestration with every side effect stubbed out."""
    set_machine_identity(role="gateway,agent-runner", name=ME)

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
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
    assert rc == 0


def _summary_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Every `[rollout-telemetry]` JSON line the orchestration printed."""
    out = capsys.readouterr().out
    return [
        json.loads(line.removeprefix("[rollout-telemetry] "))
        for line in out.splitlines()
        if line.startswith("[rollout-telemetry] {")
    ]


def test_a_clean_rollout_ends_with_the_phase_summary(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every phase the brief named is in the one aggregate line: fetch, pause,
    drain, snapshot, local stop/checkout/uv/start, readiness and Phase B."""
    _drive(monkeypatch, set_machine_identity, registered=[(ME, None), ("air", None)])  # pyright: ignore[reportUnknownArgumentType]

    summaries = _summary_lines(capsys)
    assert len(summaries) == 1
    stages = summaries[0]["stages"]
    assert isinstance(stages, dict)
    for phase in (
        "preflight",
        "phase0_fetch",
        "stop_the_world",
        "phase_a_pause",
        "quiesce_drain",
        "local_leg",
        "readiness",
        "phase_b",
    ):
        assert phase in stages, f"{phase} missing from {stages}"
    assert isinstance(summaries[0]["total_s"], float)


def test_an_abort_before_phase_a_still_prints_the_summary(
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pre-try early returns (a failed Phase-0 fetch) print the summary too —
    an aborted rollout is the one whose phase times the operator needs most."""
    _drive(monkeypatch, set_machine_identity, registered=[(ME, None)])  # pyright: ignore[reportUnknownArgumentType]
    capsys.readouterr()  # discard the clean run's output; capsys accumulates
    monkeypatch.setattr(
        _cli,
        "_fan_out",
        lambda hosts, _path, _timeout, _payload=None: [(h, "fatal", "") for h, _u in hosts],  # type: ignore[no-untyped-def]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")

    assert rc == 1
    summaries = _summary_lines(capsys)
    assert len(summaries) == 1
    stages = summaries[0]["stages"]
    assert isinstance(stages, dict)
    assert "phase0_fetch" in stages
    assert "phase_b" not in stages
