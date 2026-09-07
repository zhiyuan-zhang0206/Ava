"""`ava cluster update` restarts only the side that changed.

Two layers:
- `_classify_change`: pure path → (frontend, backend) mapping, incl. docs-only.
- `_run_gateway_orchestration`: monkeypatch the change classifier and the
  heavy helpers, assert the right path runs (frontend-only fast path skips Phase
  A / quiesce; backend-only runs the full flow with restart_frontend=False;
  docs-only pulls and restarts nothing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from cli import commands as _cli
from cli.commands import _update_orchestration as _orch
from cli.commands import update as _up


@pytest.fixture(autouse=True)
def _stub_rollout_target(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """The orchestration resolves a pinned `target_sha` (git fetch + rev-parse) and
    takes the cluster update lock before Phase A; stub both so these tests don't hit
    real git or the central-DB lock (the frontend-only / docs-only paths still acquire
    the lock via the wrapper, so the lock stub is load-bearing for them too)."""
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "TARGETSHA")
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    # the orchestration vets the target's migrations/ layout (git read) before Phase A;
    # the synthetic TARGETSHA is not a real object, so pass the vet here.
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]


class TestClassifyChange:
    def test_frontend_only(self) -> None:
        assert _up._classify_change(["ui/web/src/app/page.tsx"]) == (True, False)

    def test_backend_only(self) -> None:
        assert _up._classify_change(["gateway/app.py", "shared/db.py"]) == (False, True)

    def test_both(self) -> None:
        assert _up._classify_change(["ui/web/src/x.tsx", "agent/graph/_claim.py"]) == (True, True)

    def test_docs_only_is_neither(self) -> None:
        assert _up._classify_change(["conventions/runbook.md", "README.md"]) == (False, False)

    def test_postmortems_is_a_doc_axis(self) -> None:
        # a frozen incident narrative restarts nothing; without postmortems/ in
        # _DOC_ROOTS it falls through to "anything else" and reads as backend.
        assert _up._classify_change(["postmortems/0001-a-rollout.md"]) == (False, False)

    def test_docs_do_not_count_as_backend(self) -> None:
        # a frontend change + a top-level doc → frontend only, not backend
        assert _up._classify_change(["ui/web/x.tsx", "CLAUDE.md"]) == (True, False)

    def test_nested_md_classified_by_dir_not_as_doc(self) -> None:
        # ui/web/CLAUDE.md is under ui/web/, so it's a frontend change
        assert _up._classify_change(["ui/web/CLAUDE.md"]) == (True, False)

    def test_empty(self) -> None:
        assert _up._classify_change([]) == (False, False)


@pytest.mark.parametrize(
    ("installed_sha", "running_sha", "relation", "expected"),
    [
        ("installed-new", "running-old", "ahead", (None, True)),
        ("installed-old", "running-new", "behind", (0, True)),
    ],
)
def test_classify_rollout_replays_only_when_installed_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
    installed_sha: str,
    running_sha: str,
    relation: Literal["ahead", "behind"],
    expected: tuple[int | None, bool],
) -> None:
    """A fast-path pull advances running code; only the reverse is interrupted."""

    def _relation(_pin: str, _head: str, *, repo: Path | None = None) -> Literal["ahead", "behind"]:
        return relation

    def _persist(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("shared.source_integrity.get", lambda: installed_sha)
    monkeypatch.setattr("shared.running_sha.get", lambda: running_sha)
    monkeypatch.setattr("shared.cluster_drift.prod_source_pin_relation", _relation)
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", list)
    monkeypatch.setattr(_cli, "git_pull_main", lambda: _up.GitPullResult("a", "b", 1))
    monkeypatch.setattr(_orch, "_persist_cluster_pin", _persist)

    assert (
        _orch._classify_rollout(Path("/unused"), restart_only=False, origin="test-origin")
        == expected
    )


def test_frontend_only_takes_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """frontend-only change → _run_frontend_only_update; Phase A / quiesce /
    local update never run."""
    called: list[str] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["ui/web/src/app/page.tsx"])
    monkeypatch.setattr(
        _cli,
        "_run_frontend_only_update",
        lambda _repo, _origin: called.append("fe") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli, "_list_agent_runners", lambda: pytest.fail("must not reach Phase A on frontend-only")
    )
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: pytest.fail("must not quiesce on frontend-only"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert called == ["fe"]


def test_backend_only_runs_full_flow_without_frontend_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backend-only change → full orchestration, but local update is told NOT to
    restart the frontend (UI source unchanged)."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: captured.setdefault("quiesced", True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_run_frontend_only_update",
        lambda _repo, _origin: pytest.fail("not a frontend-only change"),  # pyright: ignore[reportUnknownArgumentType]
    )

    def _local(
        _repo: Path,
        *,
        target_sha=None,
        pull_recover=None,
        restart_frontend: bool,
        pull: bool = True,
        force_reap_agents: bool = False,
        origin: str = "",
    ) -> int:
        captured["restart_frontend"] = restart_frontend
        captured["pull"] = pull
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert captured["quiesced"] is True
    assert captured["restart_frontend"] is False


def test_both_changed_restarts_frontend_too(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        _cli, "_changed_paths_vs_origin", lambda: ["ui/web/x.tsx", "gateway/app.py"]
    )
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]

    def _local(
        _repo: Path,
        *,
        target_sha=None,
        pull_recover=None,
        restart_frontend: bool,
        pull: bool = True,
        force_reap_agents: bool = False,
        origin: str = "",
    ) -> int:
        captured["restart_frontend"] = restart_frontend
        captured["pull"] = pull
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert captured["restart_frontend"] is True


def test_docs_only_pulls_and_restarts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    pulled: list[str] = []
    pinned: list[tuple[str, str]] = []
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["conventions/runbook.md"])
    monkeypatch.setattr(
        _cli, "git_pull_main", lambda: pulled.append("pull") or _up.GitPullResult("a", "b", 1)
    )
    monkeypatch.setattr(
        _orch,
        "_persist_cluster_pin",
        lambda sha, *, origin, **_kw: pinned.append((sha, origin)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli, "_list_agent_runners", lambda: pytest.fail("docs-only must not restart anything")
    )
    monkeypatch.setattr(
        _cli,
        "_run_frontend_only_update",
        lambda _repo, _origin: pytest.fail("docs-only is not frontend"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert pulled == ["pull"]
    # The pull moved HEAD → the standing pin must advance to the pulled sha,
    # or every subsequent watchdog tick reports the gateway off-pin.
    assert pinned == [("b", "test-origin")]


def test_frontend_only_update_advances_cluster_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same pin invariant on the frontend-only fast path: after pull + frontend
    rebuild, the pulled sha becomes the cluster pin."""
    pinned: list[tuple[str, str]] = []
    monkeypatch.setattr(_up, "git_pull_main", lambda: _up.GitPullResult("OLD", "NEWSHA", 1))
    monkeypatch.setattr(_up, "_restart_frontend_session", lambda _repo: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_persist_cluster_pin",
        lambda sha, *, origin, **_kw: pinned.append((sha, origin)),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _up._run_frontend_only_update(Path("/unused"), "test-origin")
    assert rc == 0
    assert pinned == [("NEWSHA", "test-origin")]


def test_restart_only_skips_classify_pulls_nothing_and_fans_out_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--restart-only` must not classify/fetch, must call local update with pull=False
    and restart_frontend=True, and fan Phase B out with restart_only payload."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        _cli,
        "_changed_paths_vs_origin",
        lambda: pytest.fail("restart-only must not classify"),
    )
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("wsl", "http://unused")])
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: captured.setdefault("quiesced", True),  # pyright: ignore[reportUnknownArgumentType]
    )

    def _fan_out(_hosts, path, _timeout, payload=None):
        captured.setdefault("fan", []).append((path, payload))  # type: ignore[union-attr]
        return [("wsl", "ok", "")]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {"wsl": _cli.PollVerdict("ok")},  # pyright: ignore[reportUnknownArgumentType]
    )

    def _local(
        _repo: Path,
        *,
        target_sha=None,
        pull_recover=None,
        restart_frontend: bool,
        pull: bool = True,
        force_reap_agents: bool = False,
        origin: str = "",
    ) -> int:
        captured["restart_frontend"] = restart_frontend
        captured["pull"] = pull
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), restart_only=True, origin="test-origin")
    assert rc == 0
    assert captured["quiesced"] is True
    assert captured["pull"] is False
    assert captured["restart_frontend"] is True
    phase_b = [f for f in captured["fan"] if f[0] == "/api/cluster/update"]  # type: ignore[union-attr]
    assert phase_b == [("/api/cluster/update", {"restart_only": True})]


def test_classify_failure_falls_back_to_full_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git error during classification must not under-restart: fall back to the
    full flow (restart_frontend=True)."""
    captured: dict[str, object] = {}

    def _boom() -> list[str]:
        raise _up.GitPullFailed("fetch failed")

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", _boom)
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]

    def _local(
        _repo: Path,
        *,
        target_sha=None,
        pull_recover=None,
        restart_frontend: bool,
        pull: bool = True,
        force_reap_agents: bool = False,
        origin: str = "",
    ) -> int:
        captured["restart_frontend"] = restart_frontend
        captured["pull"] = pull
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert captured["restart_frontend"] is True
