# meiva:ref cli/commands/observability/docs/lgtm/lgtm.meiva.okf.md#lgtm-native-download-deadline
from __future__ import annotations

import hashlib
import http.server
import io
import re
import socketserver
import threading
import time
import urllib.request
import zipfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from base.host.net.ports import SYSTEM_URL_OPENER
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from cli.commands.observability import lgtm_native

URL = "https://example.invalid/loki.zip"

LIMITS = lgtm_native.DownloadLimits(
    total_budget_s=900.0,
    socket_timeout_s=30.0,
    attempts=3,
    retry_backoff_s=5.0,
    progress_interval_s=15.0,
)


def _limits(**overrides: float | int) -> lgtm_native.DownloadLimits:
    return lgtm_native.DownloadLimits(
        total_budget_s=float(overrides.get("total_budget_s", LIMITS.total_budget_s)),
        socket_timeout_s=float(overrides.get("socket_timeout_s", LIMITS.socket_timeout_s)),
        attempts=int(overrides.get("attempts", LIMITS.attempts)),
        retry_backoff_s=float(overrides.get("retry_backoff_s", LIMITS.retry_backoff_s)),
        progress_interval_s=float(overrides.get("progress_interval_s", LIMITS.progress_interval_s)),
    )


def test_download_with_retry_reports_used_remaining_and_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
) -> None:
    errors = [OSError(f"reset {i}") for i in range(1, 4)]
    timers = ManualTimers(start=10.0)
    opener = FakeUrlOpener()
    opener.on(URL, *errors)
    with pytest.raises(RuntimeError) as caught:
        lgtm_native.download_with_retry(
            URL, tmp_path / "loki.zip", open_url=opener, timers=timers, limits=LIMITS
        )
    assert str(caught.value) == (
        f"failed to download native LGTM backend from {URL} after 3 attempt(s) "
        f"(15s used of the 900s budget, 885s left, 0 bytes received by the last attempt): "
        f"reset 3"
    )
    assert caught.value.__cause__ is errors[-1]
    assert retry_waits == []
    assert timers.sleeps == [5.0, 10.0]
    assert [call.timeout for call in opener.calls] == [LIMITS.socket_timeout_s] * 3
    assert capsys.readouterr().err == "".join(
        f"  ! lgtm native: download attempt {i}/3 failed after "
        f"{sum(timers.sleeps[: i - 1]):.0f}s "
        f"({900.0 - sum(timers.sleeps[: i - 1]):.0f}s of the 900s budget left, "
        f"0 bytes received): reset {i}\n"
        for i in range(1, 4)
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
        return f"http://127.0.0.1:{self.server_address[1]}/loki.zip"

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


class _SilentServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SilentHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/loki.zip"

    def __enter__(self) -> _SilentServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _SilentHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        time.sleep(2.0)


@pytest.mark.parametrize("close_delimited", [False, True], ids=["content-length", "no-length"])
def test_a_dripping_mirror_cannot_outlive_the_asset_deadline(
    tmp_path: Path, close_delimited: bool
) -> None:
    limits = _limits(total_budget_s=0.5, socket_timeout_s=1.0, retry_backoff_s=0.05)
    drip = _DripServer(
        body_len=1 << 20, chunk_size=1, interval_s=0.02, close_delimited=close_delimited
    )
    opener = _ObservedOpener()
    started = time.monotonic()
    with drip, pytest.raises(RuntimeError, match="budget"):
        lgtm_native.download_with_retry(
            drip.url, tmp_path / "loki.zip", open_url=opener, timers=SYSTEM_TIMERS, limits=limits
        )
    elapsed = time.monotonic() - started
    assert elapsed >= limits.total_budget_s
    assert elapsed <= limits.total_budget_s + limits.socket_timeout_s + 0.3
    assert drip.bytes_sent < drip.body_len
    assert (opener.opened, opener.closed) == (1, 1)


def test_a_connection_that_never_sends_headers_ends_within_the_socket_guard(
    tmp_path: Path,
) -> None:
    limits = _limits(total_budget_s=0.6, socket_timeout_s=0.2, retry_backoff_s=5.0)
    server = _SilentServer()
    started = time.monotonic()
    with server, pytest.raises(RuntimeError, match="budget"):
        lgtm_native.download_with_retry(
            server.url,
            tmp_path / "loki.zip",
            open_url=_ObservedOpener(),
            timers=SYSTEM_TIMERS,
            limits=limits,
        )
    elapsed = time.monotonic() - started
    assert elapsed <= limits.total_budget_s + limits.socket_timeout_s + 0.3


def test_a_body_that_closes_after_the_deadline_is_not_a_success(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=1.0, socket_timeout_s=1.0)
    drip = _DripServer(
        body_len=2, chunk_size=1, interval_s=0.5, hold_open_s=0.2, close_delimited=True
    )
    destination = tmp_path / "loki.zip"
    with drip, pytest.raises(TimeoutError, match="budget"):
        lgtm_native.stream_download(
            drip.url,
            destination,
            open_url=_ObservedOpener(),
            timers=SYSTEM_TIMERS,
            limits=limits,
            deadline=SYSTEM_TIMERS.monotonic() + limits.total_budget_s,
            progress=lgtm_native.DownloadProgress(),
        )
    assert destination.stat().st_size == drip.body_len
    assert drip.bytes_sent == drip.body_len


def test_a_retry_wait_is_spent_from_the_same_budget(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=10.0, socket_timeout_s=30.0)
    timers = ManualTimers(start=0.0)
    opener = FakeUrlOpener()
    opener.on(URL, OSError("reset"), Answer(b"ok"))
    destination = tmp_path / "loki.zip"
    lgtm_native.download_with_retry(URL, destination, open_url=opener, timers=timers, limits=limits)
    assert destination.read_bytes() == b"ok"
    assert [call.timeout for call in opener.calls] == [10.0, 5.0]
    assert timers.sleeps == [5.0]


def test_a_wait_never_runs_past_the_deadline(tmp_path: Path) -> None:
    limits = _limits(total_budget_s=3.0, socket_timeout_s=30.0, retry_backoff_s=5.0)
    timers = ManualTimers(start=0.0)
    opener = FakeUrlOpener()
    opener.on(URL, OSError("reset"), OSError("reset"), OSError("reset"))
    with pytest.raises(RuntimeError) as caught:
        lgtm_native.download_with_retry(
            URL, tmp_path / "loki.zip", open_url=opener, timers=timers, limits=limits
        )
    assert "after 3 attempt(s) (3s used of the 3s budget, 0s left, 0 bytes received" in str(
        caught.value
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

    destination = tmp_path / "loki.zip"
    lgtm_native.stream_download(
        URL,
        destination,
        open_url=_open,
        timers=timers,
        limits=limits,
        deadline=timers.monotonic() + limits.total_budget_s,
        progress=lgtm_native.DownloadProgress(),
    )
    assert destination.read_bytes() == body
    out = capsys.readouterr().out
    assert "  · lgtm native: 0.0 MB (20%) in 4s" in out
    assert "  · lgtm native: 0.0 MB (80%) in 16s" in out
    assert out.endswith("  · lgtm native: downloaded 0.0 MB in 20s\n")
    assert opened == [min(limits.socket_timeout_s, limits.total_budget_s)]


class _FileServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, body: bytes, name: str) -> None:
        super().__init__(("127.0.0.1", 0), _FileHandler)
        self.body = body
        self.name = name

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/{self.name}"

    def __enter__(self) -> _FileServer:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()


class _FileHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_FileServer, self.server)
        self.send_response(200)
        self.send_header("Content-Length", str(len(server.body)))
        self.end_headers()
        self.wfile.write(server.body)
        self.wfile.flush()


def _zip_bytes(member: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(member, payload)
    return buffer.getvalue()


def test_an_archive_with_the_wrong_sha256_is_refused(tmp_path: Path) -> None:
    body = b"untrusted archive"
    server = _FileServer(body, "loki.zip")
    asset = {"url": server.url, "sha256": "0" * 64, "member": "loki-bin"}
    with server, pytest.raises(RuntimeError, match="SHA256 mismatch"):
        lgtm_native.download_and_verify(
            "loki",
            "3.7.6",
            asset,
            tmp_path / "native",
            tag="linux_amd64",
            open_url=SYSTEM_URL_OPENER,
            timers=SYSTEM_TIMERS,
            limits=LIMITS,
        )
    assert not (tmp_path / "native" / "version-loki").exists()


def test_a_verified_archive_is_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _zip_bytes("loki-bin", b"binary")
    server = _FileServer(body, "loki.zip")
    asset = {
        "url": server.url,
        "sha256": hashlib.sha256(body).hexdigest(),
        "member": "loki-bin",
    }
    native = tmp_path / "native"
    with server:
        lgtm_native.download_and_verify(
            "loki",
            "3.7.6",
            asset,
            native,
            tag="linux_amd64",
            open_url=SYSTEM_URL_OPENER,
            timers=SYSTEM_TIMERS,
            limits=_limits(retry_backoff_s=0.01),
        )
    assert (native / "bin/loki").read_bytes() == b"binary"
    assert (native / "bin/loki").stat().st_mode & 0o777 == 0o755
    assert (native / "version-loki").read_text(encoding="utf-8") == "3.7.6\n"
    assert (native / "platform-loki").read_text(encoding="utf-8") == "linux_amd64\n"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert re.fullmatch(
        rf"  · lgtm native: downloading .+\n  · lgtm native: downloaded {len(body) / 1e6:.1f} MB in \d+s\n",
        captured.out,
    )
