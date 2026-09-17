"""ops.ops_cluster.cluster_fetch_op — the rollout's non-disruptive fetch pre-flight.

The op's own contract (an `{"ok": ...}` dict, never an exception) is what the
gateway's fan-out reads; the thing worth pinning underneath it is that the git
calls are bounded by `run_bounded` and non-interactive. A pre-flight that leaves a
live git/ssh tail behind on a host it then declares unreachable is how the Windows
agent-runner accumulated 66 orphaned git processes.

The central-fetch refusal itself (the `fetch_via_gateway` matrix and the URL
classification) lives in `tests/ops/test_fetch_topology.py`; the op-level tests
here pin that the refusal is consulted first and that a wall source runs no fetch.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ops import fetch_topology, ops_cluster


def _completed(argv: list[str], stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")


def _stub_origin_read(monkeypatch: pytest.MonkeyPatch, origin: str) -> list[list[str]]:
    """Stub `fetch_topology.run_bounded` for the origin read; return its call log."""
    calls: list[list[str]] = []

    def _fake(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _completed(argv, stdout=origin + "\n")

    monkeypatch.setattr(fetch_topology, "run_bounded", _fake)
    return calls


def test_fetch_is_bounded_and_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git call goes through `run_bounded`, each with its own ceiling and the
    non-interactive env (origin read, fetch, resolve)."""
    _stub_origin_read(monkeypatch, "ssh://zzy@gw:2222/srv/source")
    calls: list[dict[str, Any]] = []

    def _fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return _completed(argv, stdout="deadbeef\n")

    monkeypatch.setattr(ops_cluster, "run_bounded", _fake)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is True
    assert result["fetched"] == "deadbeef"
    assert result["origin_url"] == "ssh://zzy@gw:2222/srv/source"
    assert [c["argv"] for c in calls] == [
        ["git", "fetch", "--progress", "origin"],
        ["git", "rev-parse", "origin/main"],
    ]
    assert [c["timeout"] for c in calls] == [
        ops_cluster._FETCH_TIMEOUT_S,
        ops_cluster._RESOLVE_TIMEOUT_S,
    ]
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
    _stub_origin_read(monkeypatch, "")
    calls: list[list[str]] = []

    def _fake(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _completed(argv, stdout="cafe0000\n")
        return _completed(argv)

    monkeypatch.setattr(ops_cluster, "run_bounded", _fake)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is True
    assert result["fetched"] == "cafe0000"
    assert ["git", "rev-parse", "origin/staging"] in calls


def test_fetch_refuses_a_wall_source_under_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch on + non-gateway host + a wall-source origin: the op returns the
    refusal (naming the origin) and runs NO fetch — that verdict is what aborts
    the rollout before any pause."""
    from shared.config import settings

    monkeypatch.setattr(settings.general, "fetch_via_gateway", True)
    monkeypatch.setattr(fetch_topology, "is_gateway", lambda: False)
    _stub_origin_read(monkeypatch, "git@github.com:zhiyuan-zhang0206/Ava.git")

    def _no_fetch(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"no fetch expected past the refusal, got {argv!r}")

    monkeypatch.setattr(ops_cluster, "run_bounded", _no_fetch)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is False
    assert "wall host" in str(result["error"])
    assert "github.com" in str(result["error"])
    assert result["origin_url"] == "git@github.com:zhiyuan-zhang0206/Ava.git"


def test_fetch_timeout_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout (raised after `run_bounded` has killed the tree) still surfaces
    as a failed pre-flight rather than an exception — the caller's control flow is
    untouched by the tree kill."""
    _stub_origin_read(monkeypatch, "")

    def _timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(ops_cluster, "run_bounded", _timeout)

    result = ops_cluster.cluster_fetch_op()
    assert result["ok"] is False
    assert "timed out" in str(result["error"])


def test_fetch_timeout_carries_the_partial_stderr_tail(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """A timeout must report WHERE git was when the bound tripped — `run_bounded`
    drains the tree's pipes after the kill, so `TimeoutExpired.stderr` holds the
    partial stderr (ssh connect errors, transfer progress, or nothing at all =
    died before git spoke). This is the per-attempt evidence that turns "two 30s
    timeouts then success" from a gap into a located hang (2026-08-27 forensics:
    the timeout point was previously dropped)."""
    _stub_origin_read(monkeypatch, "")

    def _timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=kwargs["timeout"],
            stderr="ssh: connect to host github.com port 22: Connection timed out",
        )

    monkeypatch.setattr(ops_cluster, "run_bounded", _timeout)

    result = ops_cluster.cluster_fetch_op()

    assert result["ok"] is False
    # The timeout point travels both in the returned error (what the gateway's
    # Phase-0 abort prints) and in the ops log line.
    assert "Connection timed out" in str(result["error"])
    messages = "\n".join(r["message"] for r in loguru_records)
    assert "[cluster_fetch] git fetch timed out" in messages
    assert "last stderr" in messages
    assert "Connection timed out" in messages


def test_fetch_logs_its_attempt_start(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Each invocation logs a start marker, so consecutive attempts (the gateway
    retries a transport timeout at its own level; each retry re-executes this op
    on the host) are countable in the ops log with their own pids."""
    _stub_origin_read(monkeypatch, "")

    def _ok(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(argv, stdout="deadbeef\n")

    monkeypatch.setattr(ops_cluster, "run_bounded", _ok)

    ops_cluster.cluster_fetch_op()

    messages = "\n".join(r["message"] for r in loguru_records)
    assert "[cluster_fetch] start" in messages
    assert "[cluster_fetch] ok" in messages


def test_fetch_nonzero_exit_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing fetch (unreachable remote, auth) is reported, not raised."""
    _stub_origin_read(monkeypatch, "")

    def _fail(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: no remote")
        return _completed(argv, stdout="deadbeef\n")

    monkeypatch.setattr(ops_cluster, "run_bounded", _fail)

    result = ops_cluster.cluster_fetch_op()
    assert result["ok"] is False
    assert "fatal: no remote" in str(result["error"])
