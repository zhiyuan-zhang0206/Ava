"""ops.ops_cluster.cluster_fetch_op — the rollout's non-disruptive fetch pre-flight.

The op's own contract (an `{"ok": ...}` dict, never an exception) is what the
gateway's fan-out reads; the thing worth pinning underneath it is that the git
calls are bounded by `run_bounded` and non-interactive. A pre-flight that leaves a
live git/ssh tail behind on a host it then declares unreachable is how the Windows
agent-runner accumulated 66 orphaned git processes.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ops import ops_cluster


def test_fetch_is_bounded_and_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both git calls go through `run_bounded`, each with its own ceiling and the
    non-interactive env."""
    calls: list[dict[str, Any]] = []

    def _fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(ops_cluster, "run_bounded", _fake)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is True
    assert result["fetched"] == "deadbeef"
    assert [c["argv"] for c in calls] == [
        ["git", "fetch", "origin"],
        ["git", "rev-parse", "origin/main"],
    ]
    assert calls[0]["timeout"] == ops_cluster._FETCH_TIMEOUT_S
    assert calls[1]["timeout"] == ops_cluster._RESOLVE_TIMEOUT_S
    for call in calls:
        assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "BatchMode=yes" in call["env"]["GIT_SSH_COMMAND"]


def test_fetch_resolves_the_track_branch_not_hardcoded_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staging/preview cluster tracks another branch (AVA_TRACK_BRANCH); the
    resolve step must read that ref, not origin/main — the old hardcoded ref
    made `fetched` permanently empty on non-main tracks."""
    from shared.config import settings

    monkeypatch.setattr(settings.general, "track_branch", "staging")
    calls: list[dict[str, Any]] = []

    def _fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="cafe0000\n", stderr="")

    monkeypatch.setattr(ops_cluster, "run_bounded", _fake)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is True
    assert result["fetched"] == "cafe0000"
    assert calls[1]["argv"] == ["git", "rev-parse", "origin/staging"]


def test_fetch_timeout_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout (raised after `run_bounded` has killed the tree) still surfaces
    as a failed pre-flight rather than an exception — the caller's control flow is
    untouched by the tree kill."""

    def _timeout(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(ops_cluster, "run_bounded", _timeout)

    result = ops_cluster.cluster_fetch_op()
    assert result["ok"] is False
    assert "timed out" in str(result["error"])


def test_fetch_nonzero_exit_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing fetch (unreachable remote, auth) is reported, not raised."""

    def _fail(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: no remote")

    monkeypatch.setattr(ops_cluster, "run_bounded", _fail)

    result = ops_cluster.cluster_fetch_op()
    assert result["ok"] is False
    assert "fatal: no remote" in str(result["error"])
