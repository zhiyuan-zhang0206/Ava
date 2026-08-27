"""Phase 0 (pre-flight fetch) must ignore an OFFLINE agent-runner, not abort the
rollout for it.

The 2026-08-03 incident (rollout log `rollout-1785804160.log`): a rollout pinned
e83c545b3 was aborted at Phase 0 because laptop-host was unreachable (the private
network was down) — `_run_preflight_fetch` treated "unreachable" and "fatal" identically and
aborted the whole rollout over one host that was never going to be touched by it.
The user ruling behind this change (Task #679): ignore an offline machine, roll
out the rest; the machine's watchdog pin-drift self-heal converges it to the pin
when it comes back.

So Phase 0 now applies the same split Phase A already applies, read off the same
statuses from the same probe:
- **unreachable** → skip + log (the host is never paused and never told to
  update, so it cannot strand; its watchdog self-heals on return);
- **fatal** (reachable, but the fetch op failed) → abort before anything pauses —
  a reachable host whose fetch fails WILL be paused by Phase A and then fail its
  Phase-B self-update, stranding it (the 2026-07-25 laptop-host incident that
  created Phase 0).
"""

from __future__ import annotations

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
    set_machine_identity,
    *,
    registered: list[tuple[str, str | None]],
    statuses: dict[str, dict[str, str]] | None = None,
) -> _Rollout:
    """Run the gateway orchestration with `registered` as the reconciled rollout
    list and this host named `ME`, recording which hosts each phase reached.

    `statuses` maps fan-out path -> host -> verdict ("ok" / "unreachable" /
    "fatal") for that phase's dial; a host with no entry answers "ok". The
    verdicts are per-path so a host can be offline for one phase and back for
    the next — each phase re-probes.
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
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda hosts, **_unused: {name: _cli.PollVerdict(_cli.POLL_OK) for name, _url in hosts},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    return _Rollout(rc, calls)


def test_phase_0_skips_an_unreachable_host_and_rolls_the_rest(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity, capsys
) -> None:
    """The incident, fixed: one offline host must not abort the rollout — it is
    skipped in Phase 0, never paused in Phase A, never told to update in Phase B,
    and the rollout finishes clean for the hosts it reached, naming the skipped
    host in its aftermath."""
    statuses = {
        "/api/cluster/fetch": {"air": "unreachable"},
        "/api/cluster/stop": {"air": "unreachable"},
        "/api/cluster/update": {"air": "unreachable"},
    }
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses=statuses,
    )

    assert rollout.rc == 0
    # air was dialed but answered unreachable in every phase — never paused,
    # never told to update.
    assert [h for h, s in rollout.results("/api/cluster/stop") if s == "ok"] == [ME, "wsl"]
    assert [h for h, s in rollout.results("/api/cluster/update") if s == "ok"] == ["wsl"]
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "unreachable and skipped" in err
    assert "air" in err


def test_phase_0_aborts_on_a_reachable_host_that_cannot_fetch(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """A REACHABLE host whose fetch fails is the case Phase 0 exists for: it WILL
    be paused by Phase A and then fail Phase B, stranding it — so the rollout
    aborts before anything is paused."""
    statuses = {"/api/cluster/fetch": {"wsl": "fatal"}}
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses=statuses,
    )

    assert rollout.rc == 1
    assert rollout.results("/api/cluster/stop") == []  # nothing was paused


def test_a_host_skipped_at_phase_0_is_not_excluded_from_later_phases(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity
) -> None:
    """Skipping is a verdict about THIS phase's dial, not a membership decision: a
    host offline at Phase 0 but back by Phase A/B is paused and updated like any
    other — each phase re-probes, so Phase 0's skip cannot drop a recovered host
    out of the rollout."""
    statuses = {"/api/cluster/fetch": {"air": "unreachable"}}
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses=statuses,
    )

    assert rollout.rc == 0
    assert [h for h, s in rollout.results("/api/cluster/stop") if s == "ok"] == [ME, "air", "wsl"]
    assert [h for h, s in rollout.results("/api/cluster/update") if s == "ok"] == ["air", "wsl"]


def test_phase_0_every_host_offline_still_rolls_the_gateway(
    monkeypatch: pytest.MonkeyPatch, set_machine_identity, capsys
) -> None:
    """The boundary case of "ignore offline machines": even when EVERY registered
    runner is unreachable, the rollout proceeds (gateway local leg + pin) and the
    skipped summary names them all — the opposite of aborting the cluster over
    its offline members."""
    statuses = {
        "/api/cluster/fetch": {"air": "unreachable", "wsl": "unreachable"},
        "/api/cluster/stop": {"air": "unreachable", "wsl": "unreachable"},
        "/api/cluster/update": {"air": "unreachable", "wsl": "unreachable"},
    }
    rollout = _drive(
        monkeypatch,
        set_machine_identity,  # pyright: ignore[reportUnknownArgumentType]
        registered=[(ME, None), ("air", None), ("wsl", None)],
        statuses=statuses,
    )

    assert rollout.rc == 0
    assert [h for h, s in rollout.results("/api/cluster/stop") if s == "ok"] == [ME]
    # Phase B still dials the registered runners (each phase re-probes) — but none
    # of them acks, so none is told to update.
    assert [h for h, s in rollout.results("/api/cluster/update") if s == "ok"] == []
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "unreachable and skipped" in err
    assert "air" in err and "wsl" in err
