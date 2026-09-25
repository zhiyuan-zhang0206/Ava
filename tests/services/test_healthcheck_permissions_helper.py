"""Watchdog probe and launchd classifier for the launchd-owned macOS helper."""

from __future__ import annotations

import json
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

# The mirror lag: the 78 exit is recorded but no LWCR marker shows in the
# properties — the exit code alone already identifies the stuck class.
_STUCK_78_NO_MARKER = """gui/501/com.ava.test.f5-lwcr-stub = {
	state = spawn scheduled
	job state = spawn failed
	properties = keepalive
	last exit code = 78: EX_CONFIG
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
    monkeypatch.setattr(hc, "_reported_unhealthy", False)


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


def test_ping_uses_short_timeout_and_helper_wire_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = _FakeSocket(
        b'{"id":0,"ok":true,"result":{"pong":true,"pid":42,"preflight_screen":true,"ax_trusted":true}}\n'
    )
    paths: list[str] = []

    def connect(path: str) -> _FakeSocket:
        paths.append(path)
        return sock

    socket_path = tmp_path / "helper.sock"
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(hc, "permissions_helper_socket", lambda: socket_path)
    monkeypatch.setattr(hc, "_helper_parent", lambda _: (object(), object()))
    monkeypatch.setattr(hc, "_parent_still_live", lambda _root, _parent, pid: pid == 42)

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
        (_STUCK_78_NO_MARKER, hc.LWCR_STUCK),  # spawn failed + 78, marker lagging
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


def test_reporting_is_episode_gated_without_repair(recorder: _Recorder) -> None:
    from shared.daemon_health import DaemonProbe

    bad = DaemonProbe.down("lwcr-stuck; needs LWCR update")
    hc.report(bad)
    hc.report(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 1
    hc.report(DaemonProbe.up("ping answered"))
    hc.report(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 2
    assert not hasattr(hc, "repair_unresponsive_helper")
    assert not hasattr(hc, "main")


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


def test_unverifiable_helper_parent_never_reports_protocol_health(monkeypatch):
    def missing_parent():
        raise hc._ParentEvidenceError("root parent is not observable")

    monkeypatch.setattr(hc, "_ping", missing_parent)
    monkeypatch.setattr(hc, "read_helper_job", lambda: _RUNNING_JOB)
    assert hc.probe().verdict.value == "unavailable"


def test_connected_helper_peer_must_be_root_native_parent(monkeypatch):
    from types import SimpleNamespace

    from services.ava_root import client as root_client
    from services.healthchecks import owned_service

    root = SimpleNamespace(pid=10, live=lambda: True)
    parent = SimpleNamespace(pid=20, live=lambda: True)
    monkeypatch.setattr(root_client, "root_process", lambda: root)
    monkeypatch.setattr(hc.psutil, "Process", lambda _: SimpleNamespace(parent=object))
    monkeypatch.setattr(hc.OwnedProcess, "capture", lambda _: parent)
    monkeypatch.setattr(owned_service, "_peer_pid", lambda _: 30)
    with pytest.raises(hc._ParentEvidenceError, match="not the captured root parent"):
        hc._helper_parent(object())
