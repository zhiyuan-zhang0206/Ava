"""Vendored relocatable Postgres: resolution + a real download/extract.

`test_pg_tool_prefers_vendored_dir` is a fast unit check (no network). The real
download test fetches the pinned zonky jar from Maven Central, extracts it, and
runs the relocatable `initdb` standalone -- the bring-up `ava start` depends on for
a brew-free machine.
"""

from __future__ import annotations

import email.message
import subprocess
import urllib.error
from pathlib import Path

import pytest

from shared import pg_tools
from shared import runtime_binaries as rb
from shared.config import settings


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the host-level runtime root at a tmp dir (via the cluster-registry
    anchor), so a test never touches the real ~/.ava/runtime."""
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))


def test_pg_tool_prefers_vendored_dir(isolated_runtime: None) -> None:
    # Absent vendored tree -> pg_tool falls back to the host (brew/apt) path.
    assert "runtime" not in str(pg_tools.pg_tool("initdb"))

    # A present vendored tree wins.
    bin_dir = rb.vendored_pg_dir() / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "initdb").write_text("#!/bin/sh\n")
    assert pg_tools.pg_tool("initdb") == bin_dir / "initdb"
    assert rb.vendored_pg_bin_dir() == bin_dir


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://repo1.invalid/x.jar", status, "boom", email.message.Message(), None
    )


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, answers: list[urllib.error.URLError | _FakeResponse]
) -> tuple[list[str], list[float]]:
    """urlopen pops `answers` (an exception raises); returns (calls, recorded sleeps)."""
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeResponse:
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, urllib.error.URLError):
            raise answer
        return answer

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", sleeps.append)
    return calls, sleeps


def test_download_retries_transient_answers_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429/5xx from the mirror and socket-level failures back off and retry; the
    sha256 pin makes the eventual bytes trustworthy regardless of which attempt won."""
    calls, sleeps = _patch_urlopen(
        monkeypatch,
        [_http_error(429), _http_error(503), urllib.error.URLError("reset"), _FakeResponse(b"jar")],
    )
    assert rb._download("https://repo1.invalid/x.jar") == b"jar"
    assert len(calls) == 4
    assert sleeps == [2, 4, 8]


def test_download_fails_fast_on_a_permanent_http_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-transient 4xx (bad pin/artifact) is a permanent answer — no retry."""
    calls, sleeps = _patch_urlopen(monkeypatch, [_http_error(404)])
    with pytest.raises(RuntimeError, match="404"):
        rb._download("https://repo1.invalid/x.jar")
    assert len(calls) == 1
    assert sleeps == []


def test_download_gives_up_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _patch_urlopen(monkeypatch, [_http_error(429)] * rb._DOWNLOAD_ATTEMPTS)
    with pytest.raises(RuntimeError, match="429"):
        rb._download("https://repo1.invalid/x.jar")
    assert len(calls) == rb._DOWNLOAD_ATTEMPTS
    assert sleeps == [2, 4, 8]


def test_ensure_pg_binaries_real_download(isolated_runtime: None) -> None:
    assert rb.vendored_pg_bin_dir() is None

    bin_dir = rb.ensure_pg_binaries()
    assert (bin_dir / "initdb").exists()
    assert rb.vendored_pg_bin_dir() == bin_dir

    # The relocatable binary runs standalone (no brew, from its extracted path).
    out = subprocess.run(  # noqa: S603 — resolved vendored initdb path + static flag
        [str(bin_dir / "initdb"), "--version"], capture_output=True, text=True, check=True
    )
    assert "17.4" in out.stdout

    # Idempotent: a second ensure is a no-op returning the same dir (no re-download).
    assert rb.ensure_pg_binaries() == bin_dir
