"""A rollout must not report success over a service it could not start.

Prod, 2026-08-06 11:16 — a frontend-only rollout:

    → restart ava-frontend (rebuild ~30-60s)
      ✗ failed to start ava-frontend
      ✓ cluster pin updated -> b667a9a
    [session-exit] rc=0

`_new_session` returned False, nobody read it, and the fast path returned 0
unconditionally. The UI was dark for three minutes and the rollout said it went fine.

What is asserted here:

- a `new-session` refusal is retried once (these are overwhelmingly transient), and
  the retry clears the name first so it cannot fail for a second reason;
- a refusal that survives the retry comes back in `LaunchOutcome.failed` rather than
  being discarded;
- the frontend-only fast path returns non-zero and prints a ROLLOUT block naming the
  session — while still advancing the pin, because the pull did land;
- the full orchestration turns a local launch failure into `INCOMPLETE` with the
  session named in the aftermath block, WITHOUT aborting the rollout: the gateway is
  serving and the agent-runners still need their update.

The single-runner-unreachable tolerance is deliberately left alone — an unreachable
host is not a local launch failure, and `test_rollout_robustness.py` owns that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands as _cli
from cli.commands import _update_recover as _rec
from cli.commands import update as _up
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


def _roster(monkeypatch: pytest.MonkeyPatch, *services: str) -> None:
    from cli.commands import _session_lifecycle as _session_mod

    annotated = tuple((_spec(s), None) for s in services)
    monkeypatch.setattr(_session_mod, "_services_for_roles_annotated", lambda _r: annotated)  # pyright: ignore[reportUnknownArgumentType]


# ── the retry ────────────────────────────────────────────────────────────────


def test_transient_launch_failure_is_retried_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One failed `new-session` is not a verdict. The retry clears the name first,
    because a first attempt that failed after claiming it would make the second fail
    on the duplicate and bury the real cause."""
    attempts: list[str] = []
    killed: list[str] = []

    def _new(session: str, _cmd: str, _cwd: Path, **_kw: object) -> bool:
        attempts.append(session)
        return len(attempts) > 1

    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_new_session", _new)
    monkeypatch.setattr(_cli, "_kill_session", lambda s, **_kw: killed.append(s) or True)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, "gateway")

    launch = _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert attempts == ["ava-gateway", "ava-gateway"]
    assert killed == ["ava-gateway"], "the retry must clear the name it is about to claim"
    assert launch.failed == ()


def test_persistent_launch_failure_is_returned_not_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Two refusals in a row is the answer, and it reaches the caller."""
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_new_session", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_kill_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    _roster(monkeypatch, "gateway", "labeler")

    launch = _cli._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert launch.failed == ("ava-gateway", "ava-labeler")
    assert [s.session for s in launch.started] == ["gateway", "labeler"]
    assert "retrying the launch once" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ── the frontend-only fast path (the incident) ───────────────────────────────


def _frontend_only_env(
    monkeypatch: pytest.MonkeyPatch, *, launch_ok: bool
) -> list[tuple[str, str]]:
    pinned: list[tuple[str, str]] = []
    monkeypatch.setattr(_up, "git_pull_main", lambda: _up.GitPullResult("OLD", "NEWSHA", 1))
    monkeypatch.setattr(_up, "_restart_frontend_session", lambda _repo: launch_ok)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_persist_cluster_pin",
        lambda sha, *, origin, **_kw: pinned.append((sha, origin)),  # pyright: ignore[reportUnknownArgumentType]
    )
    return pinned


def test_frontend_only_update_fails_loudly_when_the_session_does_not_come_up(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The incident, inverted: non-zero rc and a block naming the session."""
    _frontend_only_env(monkeypatch, launch_ok=False)

    rc = _up._run_frontend_only_update(Path("/unused"), "test-origin")

    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "ROLLOUT INCOMPLETE" in err
    assert "ava-frontend" in err
    assert "ava start" in err, "the block must carry the one command that fixes it"


def test_frontend_only_update_still_advances_the_pin_on_a_failed_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pull landed, so the code on disk IS the new commit. Leaving the pin behind
    would have the watchdog report this host off-pin once a minute, forever — a
    second, permanent failure caused by reporting the first one."""
    pinned = _frontend_only_env(monkeypatch, launch_ok=False)

    assert _up._run_frontend_only_update(Path("/unused"), "test-origin") == 1
    assert pinned == [("NEWSHA", "test-origin")]


def test_restart_frontend_session_retries_before_giving_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fast path's own relaunch gets the same one retry the roster launch does."""
    attempts: list[str] = []
    monkeypatch.setattr(_cli, "_graceful_kill_session", lambda *_a, **_kw: (True, "graceful"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_kill_session", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._repo._ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]

    def _new(session: str, _cmd: str, _cwd: Path, **_kw: object) -> bool:
        attempts.append(session)
        return len(attempts) > 1

    monkeypatch.setattr(_cli, "_new_session", _new)

    assert _up._restart_frontend_session(tmp_path) is True
    assert attempts == ["ava-frontend", "ava-frontend"]


# ── the aftermath block ──────────────────────────────────────────────────────


def test_aftermath_names_the_local_session_and_the_local_fix(capsys) -> None:
    """A rollout whose only defect is local must not read as a host problem: no host
    is stranded, so the banner and the recovery lines point at this box."""
    _rec._print_rollout_aftermath(
        reached=[],
        unreached=[],
        pin_advanced=True,
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        local_launch_failures=["ava-frontend"],
    )

    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "a service on this host did not come up" in err
    assert "ava-frontend" in err
    assert "updater log" not in err, "no host is mid-transition; do not send anyone hunting one"
    assert "ava cluster recover" not in err


def test_local_launch_failure_downgrades_the_rollout_without_aborting_it(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The full orchestration's verdict, end to end.

    The gateway landed and every agent-runner converged, so Phase B still has to run —
    aborting would leave the fleet on old code over one local session. What changes is
    the verdict: `INCOMPLETE`, rc 1, and the session named in the aftermath block."""
    from shared import launch_failures

    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("wsl", "http://unused")])
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts: {"wsl": _cli.PollVerdict("ok")},  # pyright: ignore[reportUnknownArgumentType]
    )

    fanned: list[str] = []

    def _fan_out(_hosts, path, _timeout, payload=None):
        fanned.append(path)  # pyright: ignore[reportUnknownArgumentType]
        return [("wsl", "ok", "")]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]

    def _local(_repo: Path, **_kw: object) -> int:
        # what the child `ava start` left behind: the leg itself succeeded (rc 0),
        # and the names ride the record because an exit code cannot carry them.
        launch_failures.record(["ava-frontend"])
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")

    assert rc == 1, "a rollout short a local service is not a clean rollout"
    assert "/api/cluster/update" in fanned, "Phase B must still run — the fleet needs the update"
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "ROLLOUT INCOMPLETE" in err
    assert "ava-frontend" in err
    assert launch_failures.take() == [], "the record is consumed, not left for the next rollout"


def test_aftermath_keeps_the_host_banner_when_runners_are_also_stranded(capsys) -> None:
    """Both defects at once still reads as the host problem, which is the graver one."""
    _rec._print_rollout_aftermath(
        reached=[],
        unreached=["wsl"],
        pin_advanced=True,
        outcome=_rec.RolloutOutcome.INCOMPLETE,
        local_launch_failures=["ava-frontend"],
    )

    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "some agent-runners did not" in err
    assert "ava-frontend" in err
    assert "updater log" in err
