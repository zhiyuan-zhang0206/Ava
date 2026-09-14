"""Watchdog probe and launchd classifier for the launchd-owned macOS helper."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from services.healthchecks import permissions_helper as hc
from services.permissions_helper import client

# Real `launchctl print` excerpts (F5 run-09/10c/11 evidence), shortened. The
# stuck shape deliberately keeps BOTH the top-level `state = spawn scheduled`
# line and the `job state = spawn failed` line: the classifier must read the
# latter only (PR #2500 review; the top-level `state` never carries it).
_STUCK_JOB = """gui/501/com.ava.test.f5-lwcr-stub = {
	active count = 0
	managed_by = com.apple.xpc.ServiceManagement
	state = spawn scheduled
	program = /Users/example/Applications/F5LWCRStub.app/Contents/MacOS/F5LWCRStub
	BTM uuid = 2F5F25DF-BFD3-474F-B9DF-8EBF83A685AA
	runs = 26
	last exit code = 78: EX_CONFIG
	job state = spawn failed
	properties = partial import | keepalive | runatload | needs LWCR update | has LWCR
}"""

# The lag window: `spawn failed` is already visible, the LWCR marker too, but
# launchd has not recorded the exit code yet — marker-first still classifies.
_STUCK_MARKER_ONLY = """gui/501/com.ava.test.f5-lwcr-stub = {
	state = spawn scheduled
	job state = spawn failed
	properties = keepalive | needs LWCR update | has LWCR
}"""

# Spawn failed without any LWCR mark: the next round re-classifies.
_SPAWN_FAILED_BARE = """gui/501/com.ava.test.f5-lwcr-stub = {
	state = spawn scheduled
	last exit code = 1: EPERM
	job state = spawn failed
	properties = keepalive
}"""

# Exit 78 without spawn-failed: not an LWCR verdict (78 is a generic spawn
# failure code, e.g. a missing app).
_EXIT_78_ONLY = """gui/501/com.ava.test.f5-lwcr-stub = {
	state = running
	last exit code = 78: EX_CONFIG
}"""

_RUNNING_JOB = """gui/501/com.ava.test.f5-lwcr-stub = {
	state = running
	pid = 4242
}"""


@pytest.fixture(autouse=True)
def _macos_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_consecutive_failures", 0)
    monkeypatch.setattr(hc, "_reported_unhealthy", False)
    monkeypatch.setattr(hc, "_repair_attempts", 0)
    monkeypatch.setattr(hc, "_next_repair_at", 0.0)


class _Recorder:
    """Stands in for shared.log.logger; records every structured call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def error(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import shared.log as shared_log

    rec = _Recorder()
    monkeypatch.setattr(shared_log, "logger", rec)
    return rec


class _Clock:
    """The `_monotonic` seam: advance explicitly so backoff windows elapse fast."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clk = _Clock()
    monkeypatch.setattr(hc, "_monotonic", clk)
    return clk


def _unhealthy_ping() -> bool:
    raise client.PermissionsHelperError("socket unavailable")


class _FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.timeout: float | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response

    def close(self) -> None:
        self.closed = True


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "services.healthchecks.permissions_helper"
        and record.levelno == logging.ERROR
    ]


def test_ping_uses_short_timeout_and_helper_wire_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = _FakeSocket(
        b'{"id":0,"ok":true,"result":{"pong":true,"preflight_screen":true,"ax_trusted":true}}\n'
    )
    paths: list[str] = []

    def connect(path: str) -> _FakeSocket:
        paths.append(path)
        return sock

    socket_path = tmp_path / "helper.sock"
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(hc, "permissions_helper_socket", lambda: socket_path)

    assert hc._ping()
    assert paths == [str(socket_path)]
    assert sock.timeout == 3.0
    assert json.loads(sock.sent) == {"id": 0, "method": "ping"}
    assert sock.closed


# -- parsing and the LWCR truth table (pure) ----------------------------------


def test_parse_reads_job_state_not_top_level_state() -> None:
    facts = hc.parse_job_state(_STUCK_JOB)
    assert facts.job_state == "spawn failed"
    assert facts.last_exit_code == "78: EX_CONFIG"
    assert facts.needs_lwcr_update is True
    assert facts.btm_uuid == "2F5F25DF-BFD3-474F-B9DF-8EBF83A685AA"
    assert facts.runs == 26


def test_parse_tolerates_a_missing_exit_code_and_empty_text() -> None:
    facts = hc.parse_job_state(_STUCK_MARKER_ONLY)
    assert facts.job_state == "spawn failed"
    assert facts.last_exit_code is None
    empty = hc.parse_job_state("")
    assert empty.job_state is None and empty.btm_uuid is None and empty.runs is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_STUCK_JOB, hc.LWCR_STUCK),  # spawn failed + 78 + marker
        (_STUCK_MARKER_ONLY, hc.LWCR_STUCK),  # spawn failed + marker, exit lagging
        (_SPAWN_FAILED_BARE, hc.SPAWN_FAILED),  # spawn failed, no LWCR mark
        (_EXIT_78_ONLY, hc.UNRESPONSIVE),  # 78 without spawn-failed is not LWCR
        (_RUNNING_JOB, hc.UNRESPONSIVE),  # running but not answering ping
        (None, hc.ABSENT),  # no readable job
    ],
)
def test_classification_truth_table(text: str | None, expected: str) -> None:
    job = None if text is None else hc.parse_job_state(text)
    assert hc._classify(ping_ok=False, job=job) == expected


def test_ping_alive_short_circuits_to_healthy() -> None:
    assert hc._classify(ping_ok=True, job=hc.parse_job_state(_STUCK_JOB)) == hc.HEALTHY


# -- the watchdog round (era 1) ------------------------------------------------


def test_unhealthy_reports_once_and_escalates_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Detection reports once; a failed repair escalates and retries under backoff."""
    repairs: list[None] = []

    def fail_repair() -> bool:
        repairs.append(None)
        return False

    monkeypatch.setattr(hc, "_ping", _unhealthy_ping)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _STUCK_JOB)
    monkeypatch.setattr(hc, "repair_unresponsive_helper", fail_repair)

    with caplog.at_level(logging.ERROR, logger="services.healthchecks.permissions_helper"):
        hc.main()  # 1: report once, no repair yet
        hc.main()  # 2: still counting
        assert repairs == []
        assert len(recorder.events("permissions_helper_unhealthy")) == 1

        hc.main()  # 3: first repair attempt, fails
        assert repairs == [None]
        assert len(recorder.events("permissions_helper_repair_failed")) == 1

        hc.main()  # 4: inside the backoff window, no retry
        assert repairs == [None]

    clock.advance(301.0)
    hc.main()  # 5: the retry is due — attempted again, not sealed
    assert repairs == [None, None]
    assert len(recorder.events("permissions_helper_repair_failed")) == 2
    # One detection event for the whole episode — repeats never re-report.
    assert len(recorder.events("permissions_helper_unhealthy")) == 1

    first = recorder.events("permissions_helper_repair_failed")[0]
    assert first["attempt"] == 1
    assert first["retry_s"] == 300
    assert first["classification"] == hc.LWCR_STUCK
    assert "needs LWCR update" in str(first["detail"])
    second = recorder.events("permissions_helper_repair_failed")[1]
    assert second["retry_s"] == 600  # doubled
    unhealthy = recorder.events("permissions_helper_unhealthy")[0]
    assert unhealthy["classification"] == hc.LWCR_STUCK
    assert "job state=spawn failed" in str(unhealthy["detail"])
    assert unhealthy["job_state"] == "spawn failed"
    assert unhealthy["last_exit_code"] == "78: EX_CONFIG"
    assert unhealthy["btm_uuid"] == "2F5F25DF-BFD3-474F-B9DF-8EBF83A685AA"
    assert hc._consecutive_failures == 5


def test_repair_that_raises_escalates_and_never_propagates(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    def raising_repair() -> bool:
        raise OSError("launchctl gone")

    monkeypatch.setattr(hc, "_ping", _unhealthy_ping)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _STUCK_JOB)
    monkeypatch.setattr(hc, "repair_unresponsive_helper", raising_repair)

    hc.main()
    hc.main()
    hc.main()  # must not raise; escalates instead

    assert len(recorder.events("permissions_helper_repair_failed")) == 1
    assert hc._repair_attempts == 1


def test_repair_success_is_verified_and_clears_on_the_next_round(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    replies = iter((False, False, False, True))

    def ping() -> bool:
        return next(replies)

    monkeypatch.setattr(hc, "_ping", ping)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _STUCK_JOB)
    monkeypatch.setattr(hc, "repair_unresponsive_helper", lambda: True)

    hc.main()
    hc.main()
    hc.main()  # repair answers ping

    assert hc._consecutive_failures == 3
    assert hc._reported_unhealthy is True
    assert hc._repair_attempts == 1

    hc.main()  # healthy round: the whole episode resets

    assert hc._consecutive_failures == 0
    assert hc._reported_unhealthy is False
    assert hc._repair_attempts == 0
    assert hc._next_repair_at == 0.0


def test_repair_success_then_regression_stays_one_episode(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, clock: _Clock
) -> None:
    """A repair that answered ping but regressed does not re-report; the retry ladder continues."""
    replies = iter((False, False, False, False, False))
    repairs: list[bool] = [True, False]

    def ping() -> bool:
        return next(replies)

    def repair() -> bool:
        return repairs.pop(0)

    monkeypatch.setattr(hc, "_ping", ping)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _STUCK_JOB)
    monkeypatch.setattr(hc, "repair_unresponsive_helper", repair)

    hc.main()
    hc.main()
    hc.main()  # repair 1 answers ping
    hc.main()  # ping regresses — same episode, backoff holds the retry
    clock.advance(301.0)
    hc.main()  # repair 2 fails → escalation

    assert len(recorder.events("permissions_helper_unhealthy")) == 1
    failures = recorder.events("permissions_helper_repair_failed")
    assert len(failures) == 1
    assert failures[0]["attempt"] == 2
    assert hc._repair_attempts == 2


def test_non_macos_returns_without_ping_or_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc, "_ping", lambda: pytest.fail("non-macOS must not ping the helper"))
    monkeypatch.setattr(
        hc,
        "repair_unresponsive_helper",
        lambda: pytest.fail("non-macOS must not repair launchd"),
    )
    monkeypatch.setattr(
        hc, "read_helper_job", lambda: pytest.fail("non-macOS must not read launchd")
    )

    hc.main()


# -- the root-era probe (era 2) ------------------------------------------------


def test_probe_alive_and_classified_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "_ping", lambda: True)
    assert hc.probe().alive

    monkeypatch.setattr(hc, "_ping", _unhealthy_ping)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _STUCK_JOB)
    verdict = hc.probe()
    assert not verdict.alive
    assert "lwcr-stuck" in verdict.detail
    assert "needs LWCR update" in verdict.detail

    monkeypatch.setattr(hc, "read_helper_job", lambda: None)
    assert hc.probe().detail.startswith(hc.ABSENT)


def test_probe_is_total_and_non_macos_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", False)
    assert hc.probe().alive

    monkeypatch.setattr(hc, "IS_MACOS", True)
    monkeypatch.setattr(hc, "_ping", _unhealthy_ping)

    def broken_read() -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hc, "read_helper_job", broken_read)
    verdict = hc.probe()  # never raises
    assert not verdict.alive
    assert "probe error" in verdict.detail
