# meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
from __future__ import annotations

import http.server
import re
import socketserver
import threading
import time
import urllib.request
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from base.deploy.release import collector_artifact as artifact
from base.host.net.ports import SYSTEM_URL_OPENER
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener

URL = "https://example.invalid/t.tar.gz"

LIMITS = artifact.DownloadLimits(
    total_budget_s=1800.0,
    socket_timeout_s=30.0,
    attempts=3,
    retry_backoff_s=5.0,
    progress_interval_s=15.0,
)


def _limits(**overrides: float | int) -> artifact.DownloadLimits:
    return artifact.DownloadLimits(
        total_budget_s=float(overrides.get("total_budget_s", LIMITS.total_budget_s)),
        socket_timeout_s=float(overrides.get("socket_timeout_s", LIMITS.socket_timeout_s)),
        attempts=int(overrides.get("attempts", LIMITS.attempts)),
        retry_backoff_s=float(overrides.get("retry_backoff_s", LIMITS.retry_backoff_s)),
        progress_interval_s=float(overrides.get("progress_interval_s", LIMITS.progress_interval_s)),
    )


@pytest.mark.parametrize("failures", [1, 3])
def test_download_with_retry_preserves_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
    failures: int,
) -> None:
    # meiva:link base/deploy/release/docs/collector-artifact.meiva.okf.md
    errors = [OSError(f"reset {i}") for i in range(1, failures + 1)]
    timers = ManualTimers(start=10.0)
    tarball = tmp_path / "t.tar.gz"
    opener = FakeUrlOpener()
    opener.on(URL, *errors, Answer(b"ok"))
    if failures == 3:
        with pytest.raises(RuntimeError) as caught:
            artifact.download_with_retry(
                URL, tarball, timers=timers, open_url=opener, limits=LIMITS
            )
        assert str(caught.value) == (
            f"failed to download otel-collector from {URL} after 3 attempt(s) "
            f"(15s used of the 1800s budget, 1785s left, 0 bytes received by the last attempt): "
            f"reset 3"
        )
        assert caught.value.__cause__ is errors[-1]
    else:
        artifact.download_with_retry(URL, tarball, timers=timers, open_url=opener, limits=LIMITS)
        assert tarball.read_bytes() == b"ok"
    calls = len(opener.calls)
    assert calls == min(failures + 1, 3)
    assert retry_waits == []
    assert timers.sleeps == [5.0 * i for i in range(1, calls)]
    assert [call.timeout for call in opener.calls] == [LIMITS.socket_timeout_s] * calls
    expected = "".join(
        f"  ! otel-collector: download attempt {i}/3 failed after "
        f"{sum(timers.sleeps[: i - 1]):.0f}s "
        f"({1800.0 - sum(timers.sleeps[: i - 1]):.0f}s of the 1800s budget left, "
        f"0 bytes received): reset {i}\n"
        for i in range(1, failures + 1)
    )
    captured = capsys.readouterr()
    assert captured.err == expected
    assert captured.out == (
        f"  · otel-collector: downloaded 0.0 MB in {5.0 * failures:.0f}s\n" if failures < 3 else ""
    )


class _ObservedOpener:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    def __call__(
        self, target: str | urllib.request.Request, timeout: float
    ) -> AbstractContextManager[Any]:
        inner = SYSTEM_URL_OPENER(target, timeout)
        observed = self
        self.opened += 1

        class _Response:
            def __enter__(self) -> Any:
                return inner.__enter__()

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> bool | None:
                observed.closed += 1
                return inner.__exit__(exc_type, exc_value, traceback)

        return _Response()


class _DripServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        body_len: int,
        chunk_size: int,
        interval_s: float,
        hold_open_s: float = 0.0,
        close_delimited: bool = False,
        stop_after_s: float | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _DripHandler)
        self.body_len = body_len
        self.chunk_size = chunk_size
        self.interval_s = interval_s
        self.hold_open_s = hold_open_s
        self.close_delimited = close_delimited
        self.stop_after_s = stop_after_s
        self.bytes_sent = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/t.tar.gz"

    def __enter__(self) -> _DripServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _DripHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_DripServer, self.server)
        started = time.monotonic()
        self.send_response(200)
        if server.close_delimited:
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(server.body_len))
        self.end_headers()
        sent = 0
        while sent < server.body_len:
            if server.stop_after_s is not None and time.monotonic() - started > server.stop_after_s:
                return
            size = min(server.chunk_size, server.body_len - sent)
            self.wfile.write(b"x" * size)
            self.wfile.flush()
            sent += size
            server.bytes_sent = sent
            time.sleep(server.interval_s)
        if server.hold_open_s:
            time.sleep(server.hold_open_s)


class _SilentServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *, stop_after_s: float) -> None:
        super().__init__(("127.0.0.1", 0), _SilentHandler)
        self.stop_after_s = stop_after_s

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/t.tar.gz"

    def __enter__(self) -> _SilentServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _SilentHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        time.sleep(cast(_SilentServer, self.server).stop_after_s)


@pytest.mark.parametrize("close_delimited", [False, True], ids=["content-length", "no-length"])
def test_a_dripping_mirror_cannot_outlive_the_operation_deadline(
    tmp_path: Path, close_delimited: bool
) -> None:
    limits = _limits(total_budget_s=0.5, socket_timeout_s=1.0, retry_backoff_s=0.05)
    drip = _DripServer(
        body_len=1 << 20,
        chunk_size=1,
        interval_s=0.02,
        close_delimited=close_delimited,
        stop_after_s=3.0,
    )
    opener = _ObservedOpener()
    dest = tmp_path / "t.tar.gz"
    started = time.monotonic()
    with drip, pytest.raises(RuntimeError, match="budget") as caught:
        artifact.download_with_retry(
            drip.url, dest, open_url=opener, timers=SYSTEM_TIMERS, limits=limits
        )
    elapsed = time.monotonic() - started
    assert elapsed >= limits.total_budget_s
    assert elapsed <= limits.total_budget_s + limits.socket_timeout_s + 0.3
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert drip.bytes_sent < drip.body_len
    assert (opener.opened, opener.closed) == (1, 1)


def test_a_connection_that_never_sends_headers_ends_within_the_socket_guard(
    tmp_path: Path,
) -> None:
    limits = _limits(total_budget_s=0.6, socket_timeout_s=0.2, retry_backoff_s=5.0)
    server = _SilentServer(stop_after_s=3.0)
    dest = tmp_path / "t.tar.gz"
    started = time.monotonic()
    with server, pytest.raises(RuntimeError, match="budget") as caught:
        artifact.download_with_retry(
            server.url, dest, open_url=_ObservedOpener(), timers=SYSTEM_TIMERS, limits=limits
        )
    elapsed = time.monotonic() - started
    assert elapsed <= limits.total_budget_s + limits.socket_timeout_s + 0.3
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_a_body_that_closes_after_the_deadline_is_not_a_success(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=1.0, socket_timeout_s=1.0)
    drip = _DripServer(
        body_len=2,
        chunk_size=1,
        interval_s=0.5,
        hold_open_s=0.2,
        close_delimited=True,
        stop_after_s=3.0,
    )
    dest = tmp_path / "t.tar.gz"
    with drip, pytest.raises(TimeoutError, match="budget"):
        artifact.stream_download(
            drip.url,
            dest,
            open_url=_ObservedOpener(),
            timers=SYSTEM_TIMERS,
            limits=limits,
            deadline=SYSTEM_TIMERS.monotonic() + limits.total_budget_s,
            progress=artifact.DownloadProgress(),
        )
    assert dest.stat().st_size == drip.body_len
    assert drip.bytes_sent == drip.body_len


def test_a_retry_wait_is_spent_from_the_same_budget(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=10.0, socket_timeout_s=30.0)
    timers = ManualTimers(start=0.0)
    opener = FakeUrlOpener()
    opener.on(URL, OSError("reset"), Answer(b"ok"))
    dest = tmp_path / "t.tar.gz"
    artifact.download_with_retry(URL, dest, open_url=opener, timers=timers, limits=limits)
    assert dest.read_bytes() == b"ok"
    assert [call.timeout for call in opener.calls] == [10.0, 5.0]
    assert timers.sleeps == [5.0]


def test_a_wait_never_runs_past_the_deadline(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=3.0, socket_timeout_s=30.0, retry_backoff_s=5.0)
    timers = ManualTimers(start=0.0)
    opener = FakeUrlOpener()
    opener.on(URL, OSError("reset"), OSError("reset"), OSError("reset"))
    with pytest.raises(RuntimeError) as caught:
        artifact.download_with_retry(
            URL, tmp_path / "t.tar.gz", open_url=opener, timers=timers, limits=limits
        )
    assert (
        "after 3 attempt(s) (3s used of the 3s budget, 0s left, 0 bytes received by the last "
        in (str(caught.value))
    )
    assert "exceeded its 3s budget" in str(caught.value)
    assert timers.sleeps == [3.0]
    assert len(opener.calls) == 1


class _PacedResponse:
    def __init__(self, body: bytes, timers: ManualTimers, per_read_s: float) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self._chunks = [body[i : i + 1] for i in range(len(body))]
        self._timers = timers
        self._per_read_s = per_read_s

    def read1(self, amount: int = -1) -> bytes:
        if not self._chunks:
            return b""
        self._timers.sleep(self._per_read_s)
        return self._chunks.pop(0)

    def __enter__(self) -> _PacedResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_the_progress_heartbeat_follows_the_configured_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    limits = _limits(total_budget_s=60.0, progress_interval_s=4.0)
    timers = ManualTimers(start=0.0)
    body = b"x" * 10
    opened: list[float] = []

    def _open(_url: str | urllib.request.Request, timeout: float) -> _PacedResponse:
        opened.append(timeout)
        return _PacedResponse(body, timers, per_read_s=2.0)

    dest = tmp_path / "t.tar.gz"
    artifact.stream_download(
        URL,
        dest,
        open_url=_open,
        timers=timers,
        limits=limits,
        deadline=timers.monotonic() + limits.total_budget_s,
        progress=artifact.DownloadProgress(),
    )
    assert dest.read_bytes() == body
    out = capsys.readouterr().out
    assert "  · otel-collector: 0.0 MB (20%) in 4s" in out
    assert "  · otel-collector: 0.0 MB (80%) in 16s" in out
    assert out.endswith("  · otel-collector: downloaded 0.0 MB in 20s\n")
    assert opened == [min(limits.socket_timeout_s, limits.total_budget_s)]


class _SmallFileServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, body: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _SmallFileHandler)
        self.body = body

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/otelcol-contrib.tar.gz"

    def __enter__(self) -> _SmallFileServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _SmallFileHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_SmallFileServer, self.server)
        self.send_response(200)
        self.send_header("Content-Length", str(len(server.body)))
        self.end_headers()
        self.wfile.write(server.body)
        self.wfile.flush()


def test_a_real_small_download_keeps_the_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    limits = _limits(total_budget_s=30.0, retry_backoff_s=0.01)
    body = bytes(range(256)) * 400
    server = _SmallFileServer(body)
    dest = tmp_path / "otelcol-contrib.tar.gz"
    with server:
        artifact.download_with_retry(
            server.url, dest, open_url=SYSTEM_URL_OPENER, timers=SYSTEM_TIMERS, limits=limits
        )
    assert dest.read_bytes() == body
    captured = capsys.readouterr()
    assert captured.err == ""
    assert re.fullmatch(
        rf"  · otel-collector: downloaded {len(body) / 1e6:.1f} MB in \d+s\n", captured.out
    )
