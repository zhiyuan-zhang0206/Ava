"""ops.fetch_topology — where the cluster source fetch may point (central fetch).

Two facts worth pinning independently of the op that consults them: the host
classification (URL forms, lookalike hosts) and the refusal matrix (switch x
gateway capability x wall source). The refusal is the "no silent wall fallback"
enforcement of the central-fetch topology.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ops import fetch_topology
from shared.config import settings


def test_wall_source_classification() -> None:
    """Both URL styles are parsed to a host — a path that merely mentions
    github.com does not count, and lookalike hosts do not match."""
    assert fetch_topology._wall_source("git@github.com:zhiyuan-zhang0206/Ava.git")
    assert fetch_topology._wall_source("https://github.com/zhiyuan-zhang0206/Ava.git")
    assert fetch_topology._wall_source("ssh://git@ssh.github.com:443/zhiyuan-zhang0206/ava.git")
    assert not fetch_topology._wall_source("ssh://zzy@10.0.0.5:2222/home/zzy/.ava/source")
    assert not fetch_topology._wall_source("")
    assert not fetch_topology._wall_source("https://github.com.evil.example/x")
    assert not fetch_topology._wall_source("/Users/someone/.ava/mirrors/AvaSource.git")


@pytest.mark.parametrize(
    ("via_gateway", "is_gw", "origin", "refused"),
    [
        (True, False, "git@github.com:o/r.git", True),
        (True, False, "ssh://git@ssh.github.com:443/o/r.git", True),
        (True, False, "ssh://zzy@gw:2222/srv/source", False),
        (True, False, "", False),
        (True, True, "git@github.com:o/r.git", False),  # the gateway may cross the wall
        (False, False, "git@github.com:o/r.git", False),  # switch off: today's behavior
    ],
)
def test_fetch_wall_refusal_matrix(
    monkeypatch: pytest.MonkeyPatch, via_gateway: bool, is_gw: bool, origin: str, refused: bool
) -> None:
    monkeypatch.setattr(settings.general, "fetch_via_gateway", via_gateway)
    monkeypatch.setattr(fetch_topology, "is_gateway", lambda: is_gw)

    refusal = fetch_topology.fetch_wall_refusal(origin)

    if refused:
        assert refusal is not None
        assert "fetch_via_gateway" in refusal
        assert "wall" in refusal and "github.com" in refusal
        assert "fetch-via-gateway-runbook.md" in refusal
    else:
        assert refusal is None


def test_git_origin_url_reads_and_strips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def _fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout=" ssh://gw/source \n", stderr="")

    monkeypatch.setattr(fetch_topology, "run_bounded", _fake)

    assert fetch_topology.git_origin_url(tmp_path) == "ssh://gw/source"
    assert calls[0]["argv"] == ["git", "remote", "get-url", "origin"]
    assert calls[0]["timeout"] == fetch_topology._ORIGIN_READ_TIMEOUT_S
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_git_origin_url_unreadable_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="no such remote")

    monkeypatch.setattr(fetch_topology, "run_bounded", _fail)

    assert fetch_topology.git_origin_url(tmp_path) == ""
