"""The Phase-B poll's per-host updater-stage harvest (Task #1820).

A converged host's completed stage breakdown — `start` included — lands in its
updater log only after the posture row goes idle, so the probe that sees the
host resume usually carries it (the fresh-idle read) but can beat it by
milliseconds. These pin the poll's three responses: capture what each probe
carries, re-probe once after a short grace when the final `start` stage is
missing, and never let a failed harvest change the verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cli import commands as _cli
from ops import cluster_rpc as cr
from shared.host_deploy_state import HostDeployState

HOST = "wsl"


@pytest.fixture
def poll_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, int], list[dict[str, object]], dict[str, int]]:
    """Short poll constants + a recorded dispatch queue; the fixture's read
    flips the posture to idle on the probe number in `idle_at` (default: after
    the queued responses are exhausted)."""
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr("cli.commands._update_phase_b.HARVEST_GRACE_S", 0.01)
    calls: dict[str, int] = {"n": 0}
    responses: list[dict[str, object]] = []
    idle_at: dict[str, int] = {"n": 0}

    async def _dispatch(*, target_machine, kind, payload, timeout_s, ops_url=None):  # type: ignore[no-untyped-def]
        assert kind == "status_probe"
        calls["n"] += 1
        return responses[min(calls["n"], len(responses)) - 1]

    def _fake_read(machine=None, **_kw):  # type: ignore[no-untyped-def]
        threshold = idle_at["n"] or len(responses)
        return HostDeployState(
            machine=machine or HOST,  # pyright: ignore[reportUnknownArgumentType]
            posture="paused" if calls["n"] < threshold else "idle",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
            paused_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _dispatch)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    return calls, responses, idle_at


def _outcome(stages: dict[str, float]) -> dict[str, object]:
    return {"kind": "exited", "rc": 0, "stages": stages}


def test_the_convergence_probe_carries_the_completed_stages(
    poll_seams: tuple[dict[str, int], list[dict[str, object]], dict[str, int]],
) -> None:
    """The normal path: the probe that sees the host resume carries the full
    breakdown (`start` included, via the fresh-idle read) — no harvest probe
    needed."""
    calls, responses, _idle_at = poll_seams
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2})})
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2, "start": 14.6})})
    host_outcomes: dict[str, dict[str, float]] = {}

    out = _cli._poll_until_unpaused([(HOST, "http://unused")], host_outcomes=host_outcomes)

    assert {n: v.status for n, v in out.items()} == {HOST: _cli.POLL_OK}
    assert host_outcomes[HOST] == {"checkout": 3.2, "start": 14.6}
    assert calls["n"] == 2  # no harvest re-probe: the start stage was already there


def test_a_missing_start_stage_triggers_one_harvest_probe(
    poll_seams: tuple[dict[str, int], list[dict[str, object]], dict[str, int]],
) -> None:
    """The race: the convergence probe beats the updater's final `start` line by
    milliseconds. The poll re-probes once after a short grace — the fresh-idle
    read then serves the completed breakdown."""
    calls, responses, idle_at = poll_seams
    idle_at["n"] = 2
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2})})
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2})})
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2, "start": 14.6})})
    host_outcomes: dict[str, dict[str, float]] = {}

    out = _cli._poll_until_unpaused([(HOST, "http://unused")], host_outcomes=host_outcomes)

    assert {n: v.status for n, v in out.items()} == {HOST: _cli.POLL_OK}
    assert host_outcomes[HOST] == {"checkout": 3.2, "start": 14.6}
    assert calls["n"] == 3  # exactly one harvest probe


def test_a_fast_host_is_served_by_the_fresh_idle_read(
    poll_seams: tuple[dict[str, int], list[dict[str, object]], dict[str, int]],
) -> None:
    """A host that converges before the poll's first probe ever reaches it
    carries nothing mid-run; the fresh-idle read serves its completed stages on
    the first probe that finds it converged."""
    calls, responses, _idle_at = poll_seams
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2, "start": 14.6})})
    host_outcomes: dict[str, dict[str, float]] = {}

    out = _cli._poll_until_unpaused([(HOST, "http://unused")], host_outcomes=host_outcomes)

    assert {n: v.status for n, v in out.items()} == {HOST: _cli.POLL_OK}
    assert host_outcomes[HOST] == {"checkout": 3.2, "start": 14.6}
    assert calls["n"] == 1


def test_the_harvest_is_best_effort(
    poll_seams: tuple[dict[str, int], list[dict[str, object]], dict[str, int]],
) -> None:
    """A harvest probe that comes back empty changes nothing about the verdict —
    the host converged; the summary just keeps the last stages it did carry."""
    calls, responses, idle_at = poll_seams
    idle_at["n"] = 2
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2})})
    responses.append({"last_updater_outcome": _outcome({"checkout": 3.2})})
    responses.append({})  # harvest probe: no outcome at all
    host_outcomes: dict[str, dict[str, float]] = {}

    out = _cli._poll_until_unpaused([(HOST, "http://unused")], host_outcomes=host_outcomes)

    assert {n: v.status for n, v in out.items()} == {HOST: _cli.POLL_OK}
    assert host_outcomes[HOST] == {"checkout": 3.2}
    assert calls["n"] == 3
