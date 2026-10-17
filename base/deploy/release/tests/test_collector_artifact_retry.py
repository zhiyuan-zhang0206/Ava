# meiva:ref base/deploy/release/docs/collector-artifact.meiva.okf.md#bounded-download-attempt
from __future__ import annotations

import http.server
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

CAP_S = 1.0


@pytest.mark.parametrize("failures", [1, 3])
def test_download_with_retry_preserves_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
    failures: int,
) -> None:
    # meiva:ref base/deploy/release/docs/collector-artifact.meiva.okf.md#download-wait-closure
    errors = [OSError(f"reset {i}") for i in range(1, failures + 1)]
    timers = ManualTimers(start=10.0)
    url = "https://example.invalid/t.tar.gz"
    opener = FakeUrlOpener()
    opener.on(url, *errors, Answer(b"ok"))
    if failures == 3:
        with pytest.raises(RuntimeError) as caught:
            artifact.download_with_retry(url, tmp_path / "t.tar.gz", timers=timers, open_url=opener)
        assert str(caught.value) == (
            f"failed to download otel-collector from {url} after 3 attempts "
            f"({sum(timers.sleeps):.0f}s total): reset 3"
        )
        assert caught.value.__cause__ is errors[-1]
    else:
        artifact.download_with_retry(url, tmp_path / "t.tar.gz", timers=timers, open_url=opener)
        assert (tmp_path / "t.tar.gz").read_bytes() == b"ok"
    calls = len(opener.calls)
    assert calls == min(failures + 1, 3)
    assert retry_waits == []
    assert timers.sleeps == [5.0 * i for i in range(1, calls)]
    expected = "".join(
        f"  ! otel-collector: download attempt {i}/3 failed after {sum(timers.sleeps[: i - 1]):.0f}s: reset {i}\n"
        for i in range(1, failures + 1)
    )
    captured = capsys.readouterr()
    assert captured.err == expected
    assert captured.out == ("  · otel-collector: downloaded 0.0 MB in 0s\n" if failures < 3 else "")
    assert all(call.timeout == artifact.DOWNLOAD_SOCKET_TIMEOUT_S for call in opener.calls)


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
    ) -> None:
        super().__init__(("127.0.0.1", 0), _DripHandler)
        self.body_len = body_len
        self.chunk_size = chunk_size
        self.interval_s = interval_s
        self.hold_open_s = hold_open_s
        self.close_delimited = close_delimited
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
        self.send_response(200)
        if server.close_delimited:
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(server.body_len))
        self.end_headers()
        sent = 0
        while sent < server.body_len:
            size = min(server.chunk_size, server.body_len - sent)
            self.wfile.write(b"x" * size)
            self.wfile.flush()
            sent += size
            server.bytes_sent = sent
            time.sleep(server.interval_s)
        if server.hold_open_s:
            time.sleep(server.hold_open_s)


def test_trickling_body_cannot_stretch_the_wall_clock_cap(tmp_path: Path) -> None:
    drip = _DripServer(body_len=1 << 20, chunk_size=16, interval_s=0.01)
    opener = _ObservedOpener()
    dest = tmp_path / "t.tar.gz"
    started = time.monotonic()
    with drip, pytest.raises(TimeoutError, match="wall-clock cap"):
        artifact.stream_download(
            drip.url,
            dest,
            open_url=opener,
            timers=SYSTEM_TIMERS,
            attempt_timeout_s=CAP_S,
        )
    elapsed = time.monotonic() - started
    assert elapsed <= CAP_S + artifact.DOWNLOAD_SOCKET_TIMEOUT_S
    assert drip.bytes_sent < drip.body_len
    assert (opener.opened, opener.closed) == (1, 1)


def test_empty_read_that_ends_the_body_is_checked(tmp_path: Path) -> None:
    drip = _DripServer(
        body_len=64 << 10, chunk_size=8192, interval_s=0.005, hold_open_s=1.5, close_delimited=True
    )
    opener = _ObservedOpener()
    dest = tmp_path / "t.tar.gz"
    with drip, pytest.raises(TimeoutError, match="wall-clock cap"):
        artifact.stream_download(
            drip.url,
            dest,
            open_url=opener,
            timers=SYSTEM_TIMERS,
            attempt_timeout_s=CAP_S,
        )
    assert dest.stat().st_size == drip.body_len
    assert drip.bytes_sent == drip.body_len
    assert (opener.opened, opener.closed) == (1, 1)
