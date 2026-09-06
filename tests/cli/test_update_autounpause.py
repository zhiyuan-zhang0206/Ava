"""Compensating unpause: `_run_gateway_orchestration` must resume the
agent-runners it paused whenever the rollout aborts — or a host fails to self-update
in Phase B — so none stay stranded `cluster_paused` (the 2026-06-01 incident; a
paused host's watchdog skips its own self-heal, so the orchestration must cover
it). Mirrors the monkeypatch seams used in tests/cli/test_update_quiesce.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import _update_recover as _rec
from cli.commands import update as _up


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """The orchestration resolves a pinned `target_sha` (git fetch + rev-parse) and
    takes the cluster update lock before Phase A; stub both so these tests don't hit
    real git or the central-DB lock (and never contend on it under xdist)."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    # the orchestration vets the target's migrations/ layout (git read) before Phase A;
    # the synthetic TARGETSHA is not a real object, so pass the vet here.
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]


def _record_fan_out(calls: list[tuple[str, list[str]]]):
    """A `_fan_out` stub that records (path, host-names) and returns all-ok."""

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        calls.append((path, [h[0] for h in hosts]))  # pyright: ignore[reportUnknownArgumentType]
        return [(name, "ok", "") for name, _url in hosts]

    return _fan_out


def test_phase_a_fatal_resumes_every_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase A 5xx aborts pre-migration. The finally resumes every host the run may
    have paused — resume is idempotent, so a host whose pause-ack was lost (the
    'fatal' one here) must still be covered; rc=1; quiesce never runs."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None), ("b", None)])

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        calls.append((path, [h[0] for h in hosts]))  # pyright: ignore[reportUnknownArgumentType]
        if path == "/api/cluster/stop":
            return [("a", "ok", ""), ("b", "fatal", "boom")]
        return [(h[0], "ok", "") for h in hosts]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: pytest.fail("must abort before quiesce"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [
        ("/api/cluster/resume", ["a", "b"])
    ], calls


def test_local_update_failure_resumes_every_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local update fails (the 04:20 scenario) -> every paused host resumed."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None), ("b", None)])
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [
        ("/api/cluster/resume", ["a", "b"])
    ], calls


def test_gateway_local_finally_finalizes_the_pause_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression (2026-08-26): the finally's local unpause is the co-located
    host's resume, so it must finalize the pause-owner journal like a
    cluster/resume op would — otherwise the journal stays `paused` forever while
    the rollout reports rc=0 (deploy-pause-owner.json still paused)."""
    from datetime import UTC, datetime

    from shared import pause_owner

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    acquired = datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC)
    from shared.cluster_lock import DeployLease

    monkeypatch.setattr(_up, "self_holder", lambda: "macmini:pid65276")
    monkeypatch.setattr(
        _up,
        "read_update_lease",
        lambda: DeployLease(
            holder="macmini:pid65276",
            held_for_s=0,
            expires_in_s=60,
            note=None,
            kind="rollout",
            acquired_at=acquired,
        ),
    )
    pause_owner.mark_paused("macmini:pid65276", acquired)

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {"a": _cli.PollVerdict(_cli.POLL_OK)},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    snapshot = pause_owner.read()
    assert snapshot.status == "resumed"
    assert snapshot.matches("macmini:pid65276", acquired)


def test_gateway_local_finally_finalizes_the_journal_on_abort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The abort path runs the same finally, so the local journal is finalized
    there too — a co-located host Phase A paused must not keep a `paused`
    journal after the rollout gave up."""
    from datetime import UTC, datetime

    from shared import pause_owner

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    acquired = datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC)
    from shared.cluster_lock import DeployLease

    monkeypatch.setattr(_up, "self_holder", lambda: "macmini:pid65276")
    monkeypatch.setattr(
        _up,
        "read_update_lease",
        lambda: DeployLease(
            holder="macmini:pid65276",
            held_for_s=0,
            expires_in_s=60,
            note=None,
            kind="rollout",
            acquired_at=acquired,
        ),
    )
    pause_owner.mark_paused("macmini:pid65276", acquired)

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])

    def _fatal_stop(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        if path == "/api/cluster/stop":
            return [(name, "fatal", "boom") for name, _url in hosts]
        return [(name, "ok", "") for name, _url in hosts]

    monkeypatch.setattr(_cli, "_fan_out", _fatal_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: pytest.fail("must abort before quiesce"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert pause_owner.read().status == "resumed"


def test_gateway_local_finally_swallows_a_finalize_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finalize runs in a `finally` that may already be unwinding; a journal
    write failure must be swallowed, never allowed to mask the rollout outcome
    (the 2026-07-20 never-raise-in-the-finally contract)."""
    from shared import pause_owner

    def _boom() -> bool:
        raise OSError("journal write failed")

    monkeypatch.setattr(pause_owner, "finalize_natural_resume", _boom)
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {"a": _cli.PollVerdict(_cli.POLL_OK)},  # pyright: ignore[reportUnknownArgumentType]
    )

    # must not raise out of the finally despite the journal write failing
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0


def test_success_path_does_not_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """All hosts poll back paused=false -> Phase B was the natural resume -> the
    finally sends nothing (must not re-resume a host that self-updated)."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None), ("b", None)])
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {"a": _cli.PollVerdict("ok"), "b": _cli.PollVerdict("ok")},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [], calls


def test_success_path_persists_cluster_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the gateway local update succeeds, the orchestration persists the
    resolved target_sha as the standing cluster pin (cluster_target_sha). Single-host
    path (no agent-runners) so it returns right after the pin write."""
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    pinned: list[str] = []
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda sha, **_kw: pinned.append(sha))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert pinned == ["TARGETSHA"]


def test_restart_only_does_not_persist_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--restart-only` bounces the current code (target_sha None) — it pins nothing."""
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    pinned: list[str] = []
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda sha, **_kw: pinned.append(sha))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), restart_only=True, origin="test-origin")
    assert rc == 0
    assert pinned == []


def test_phase_b_unconverged_host_is_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host the Phase B poll still reports paused did NOT self-resume, and a paused
    host's watchdog skips its self-heal — so the finally resumes exactly it, not the
    host that came back ok. The rollout itself reports INCOMPLETE (rc=1): the gateway
    landed and this host did not, which is not a clean finish."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None), ("b", None)])
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {  # pyright: ignore[reportUnknownArgumentType]
            "a": _cli.PollVerdict(_cli.POLL_OK),
            "b": _cli.PollVerdict(_cli.POLL_CONVERGING),
        },
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [
        ("/api/cluster/resume", ["b"])
    ], calls


def test_unexpected_exception_after_phase_a_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise after Phase A (e.g. quiesce blows up) still triggers compensation."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    monkeypatch.setattr(_cli, "_fan_out", _record_fan_out(calls))  # pyright: ignore[reportUnknownArgumentType]

    def _boom(**kwargs: object) -> None:
        raise RuntimeError("quiesce blew up")

    monkeypatch.setattr(_cli, "_quiesce_all_agents", _boom)
    with pytest.raises(RuntimeError, match="quiesce blew up"):
        _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert [c for c in calls if c[0] == "/api/cluster/resume"] == [
        ("/api/cluster/resume", ["a"])
    ], calls


def _record_full_fan_out(resume_hosts: list[tuple[str, str | None]]):
    """A `_fan_out` stub that records the FULL (name, ops_url) tuples the resume
    fan-out receives (not just names), so a test can assert the pre-resolved URLs
    are threaded through — the pg-free compensation contract."""

    def _fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        if path == "/api/cluster/resume":
            resume_hosts.extend(hosts)  # pyright: ignore[reportUnknownArgumentType]
        return [(name, "ok", "") for name, _url in hosts]

    return _fan_out


def test_compensation_resume_carries_preresolved_ops_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a full abort, the compensating resume dials each host at its pre-resolved
    ops URL (captured from the `machines` read while Postgres was up), so it stays
    Postgres-free when a failed local update took the data plane down — the
    2026-07-20 incident. Assert the resume fan-out receives the URLs, not None."""
    resume_hosts: list[tuple[str, str | None]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(
        _cli, "_list_agent_runners", lambda: [("a", "http://a:8106"), ("b", "http://b:8106")]
    )
    monkeypatch.setattr(_cli, "_fan_out", _record_full_fan_out(resume_hosts))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert resume_hosts == [("a", "http://a:8106"), ("b", "http://b:8106")]


def test_phase_b_unconverged_resume_preserves_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The post-Phase-B narrowing (to still-paused hosts) preserves each host's
    pre-resolved ops URL via the runner_urls map — so even the narrowed resume is
    Postgres-free, not a `(name, None)` that would force a re-lookup."""
    resume_hosts: list[tuple[str, str | None]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(
        _cli, "_list_agent_runners", lambda: [("a", "http://a:8106"), ("b", "http://b:8106")]
    )
    monkeypatch.setattr(_cli, "_fan_out", _record_full_fan_out(resume_hosts))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda _repo, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {  # pyright: ignore[reportUnknownArgumentType]
            "a": _cli.PollVerdict(_cli.POLL_OK),
            "b": _cli.PollVerdict(_cli.POLL_STALLED),
        },
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
    assert resume_hosts == [("b", "http://b:8106")]


# --- finalize_rollout: safe compensation + residual-state report -------------


def test_finalize_rollout_prints_aftermath_on_abnormal_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On an abnormal exit the finally prints a residual-state + recovery block so an
    operator does not reverse-engineer the cluster state from a traceback: paused
    hosts, whether the pin advanced, and the manual-recovery commands."""
    calls: list[tuple[str, list[str]]] = []
    _rec.finalize_rollout(
        [("a", "http://a:8106")],
        _record_fan_out(calls),  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.ABORTED,
        pin_advanced=False,
    )
    err = capsys.readouterr().err
    assert calls == [("/api/cluster/resume", ["a"])]
    assert "ROLLOUT ABORTED" in err
    assert "NOT advanced" in err
    assert "ava cluster update" in err  # the retry command is in the recovery block


def test_finalize_rollout_silent_on_clean_exit(capsys: pytest.CaptureFixture[str]) -> None:
    """A clean finish still resumes any host Phase B left paused, but must NOT print
    any residual-state summary (the rollout succeeded overall)."""
    calls: list[tuple[str, list[str]]] = []
    _rec.finalize_rollout(
        [("b", "http://b:8106")],
        _record_fan_out(calls),  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.CLEAN,
        pin_advanced=True,
    )
    err = capsys.readouterr().err
    assert calls == [("/api/cluster/resume", ["b"])]  # still resumes
    assert "ROLLOUT ABORTED" not in err
    assert "ROLLOUT INCOMPLETE" not in err


def test_finalize_rollout_incomplete_is_not_reported_as_aborted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An INCOMPLETE rollout gets its own banner and its own advice. It is neither a
    clean finish (measured on prod: `[session-exit] rc=0` while three of four runners
    never came back) nor an abort — the gateway migrated and the pin advanced, so the
    ABORTED block's "re-run `ava cluster update` to retry" is the 2026-07-29 collision."""
    calls: list[tuple[str, list[str]]] = []
    _rec.finalize_rollout(
        [("win", "http://win:8106")],
        _record_fan_out(calls),  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        pin_advanced=True,
    )
    err = capsys.readouterr().err
    assert "ROLLOUT INCOMPLETE" in err
    assert "ROLLOUT ABORTED" not in err
    assert "do NOT re-run `ava cluster update` yet" in err
    assert "advanced" in err


def test_the_pin_line_does_not_promise_a_self_heal_to_a_still_paused_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #1114, in the banner. This block names the hosts the compensating resume
    could not reach as STILL PAUSED, and one line later told the operator that every
    remaining host "converges via Phase B / their watchdog self-heal". A paused host
    reconciles nothing — `PauseController` blocks the round ahead of pin and code — so
    for exactly the hosts listed above it, that sentence is the one promise this block
    cannot make, and it reads as "wait"."""

    def _failing_fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        return [(name, "unreachable", "connection refused") for name, _url in hosts]

    _rec.finalize_rollout(
        [("win", "http://win:8106")],
        _failing_fan_out,  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        pin_advanced=True,
    )
    err = capsys.readouterr().err
    assert "STILL PAUSED" in err and "'win'" in err
    assert "advanced" in err  # the pin fact itself is unchanged
    assert "skips every round while the pause holds" in err
    assert "stranded-pause recovery" in err
    assert "unpause its posture row" in err  # and the operator's own way out


def test_the_pin_line_quotes_no_recovery_deadline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No duration is printed, and that is a decision rather than an omission. How long
    the flag stands is not one number — stranded-pause recovery declines while ANYTHING
    owns the pause, and a rollout ending INCOMPLETE has just taken a deploy hold over
    these very hosts. A bound here would replace one wrong promise with another, so the
    line names the dependency and points at `ava cluster status` instead."""

    def _failing_fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        return [(name, "unreachable", "connection refused") for name, _url in hosts]

    _rec.finalize_rollout(
        [("win", "http://win:8106")],
        _failing_fan_out,  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        pin_advanced=True,
    )
    err = capsys.readouterr().err
    pin_line = next(ln for ln in err.splitlines() if "cluster pin:" in ln)
    assert not re.search(r"\d+\s*(m|min|s|sec)\b", pin_line), pin_line
    assert "waits until nothing owns the pause" in pin_line
    assert "ava cluster status" in pin_line


def test_the_pin_line_is_unchanged_when_every_host_was_resumed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The plain claim is true of a host the resume DID reach: unpaused, off-pin, its
    watchdog's pin/code controllers converge it without an operator. Keeping it for that
    case is why this is a scoping fix and not a rewrite."""
    calls: list[tuple[str, list[str]]] = []
    _rec.finalize_rollout(
        [("win", "http://win:8106")],
        _record_fan_out(calls),  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        pin_advanced=True,
    )
    err = capsys.readouterr().err
    assert "STILL PAUSED" not in err
    assert "converge via Phase B / their watchdog self-heal" in err
    assert "stranded-pause recovery" not in err


def test_finalize_rollout_never_raises_when_resume_dial_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 2026-07-20 incident: the compensating resume itself raised (its Postgres
    read), burying the root cause under a second traceback. finalize_rollout must
    swallow that, flag the hosts as still paused, and still print the summary."""

    def _raising_fan_out(hosts, path, _timeout, payload=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("psycopg OperationalError: connection refused")

    # must not raise
    _rec.finalize_rollout(
        [("a", "http://a:8106")],
        _raising_fan_out,  # pyright: ignore[reportUnknownArgumentType]
        10.0,
        deploy_capability={"deploy_holder": "g", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
        outcome=_rec.RolloutOutcome.ABORTED,
        pin_advanced=False,
    )
    err = capsys.readouterr().err
    assert "compensating resume dial itself failed" in err
    assert "STILL PAUSED" in err
    assert "'a'" in err
