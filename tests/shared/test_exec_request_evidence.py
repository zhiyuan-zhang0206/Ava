"""Incarnation attribution and process proof for leftover exec request envelopes —
and the bounded disposition of the ones whose own bytes cannot be read (D-2)."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psutil
import pytest

from shared import exec_request_evidence
from shared.exec_request_evidence import (
    Verdict,
    main,
    quarantine_stale,
    survey,
)
from shared.incarnation_resources import IncarnationResources, ResourceProcess
from shared.runtime_incarnation import RuntimeIncarnation

_AGENT = 424242


def _write_envelope(
    exec_dir: Path,
    name: str = "req-unit.json",
    *,
    agent_id: int = _AGENT,
    owner: object | None = None,
    timeout_s: float = 30.0,
    payload: dict[str, object] | None = None,
    age_s: float = 0.0,
) -> Path:
    """One request envelope; `age_s` moves it outside any live birth window."""
    target = exec_dir / str(agent_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    envelope: dict[str, object] = {
        "v": 1,
        "code": "print('x')",
        "agent_id": agent_id,
        "timeout_s": timeout_s,
    }
    if owner is not None:
        envelope["incarnation"] = {"generation": str(uuid4()), "owner": str(owner)}
    target.write_text(json.dumps(payload or envelope))
    if age_s:
        stamp = target.stat().st_mtime - age_s
        os.utime(target, (stamp, stamp))
    return target


def _superseded() -> RuntimeIncarnation:
    return RuntimeIncarnation(_AGENT, uuid4(), uuid4())


def _only_process(process: Mock) -> Callable[[list[str]], Iterator[Mock]]:
    """A `psutil.process_iter` stand-in yielding exactly one fake process."""

    def iter_processes(_attrs: list[str]) -> Iterator[Mock]:
        return iter([process])

    return iter_processes


def _resources(host: ResourceProcess) -> dict[str, object]:
    return IncarnationResources(
        generation=uuid4(), owner=uuid4(), host_process=host, requests={}
    ).model_dump(mode="json")


@pytest.fixture
def exec_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "exec"
    directory.mkdir()
    monkeypatch.setattr(exec_request_evidence, "exec_run_dir", lambda: directory)
    return directory


@pytest.fixture
def quarantine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "quarantined-exec-requests"
    monkeypatch.setattr(exec_request_evidence, "quarantined_exec_requests_dir", lambda: directory)
    return directory


def test_superseded_envelope_is_stale_and_quarantined_with_receipt(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """An old incarnation's envelope moves aside; its bytes and proof survive."""
    owner = uuid4()
    request = _write_envelope(exec_dir, owner=owner, age_s=3600)
    before = request.read_text()
    incumbent = _superseded()

    (entry,) = survey(_AGENT, incumbent=incumbent, resources=None)
    assert entry.verdict is Verdict.STALE
    assert entry.incarnation is not None and entry.incarnation.owner == owner
    assert entry.live_pids == ()

    report = quarantine_stale(_AGENT, incumbent=incumbent, resources=None, reason="unit test")
    assert report.retained == ()
    assert not request.exists()
    (moved,) = report.quarantined
    assert moved.destination.read_text() == before
    assert moved.destination.parent.parent.parent == quarantine_dir
    receipt = json.loads((moved.destination.parent / "receipt.json").read_text())
    (record,) = receipt["entries"]
    assert receipt["reason"] == "unit test"
    assert record["source"] == str(request)
    assert record["destination"] == str(moved.destination)
    assert record["owner"] == str(owner) and record["verdict"] == "stale"


def test_unattributed_envelope_is_retained_and_never_moved(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """Absent structured evidence stays exactly where it is, with its reason."""
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "no incarnation" in entry.detail
    assert request.exists() and not quarantine_dir.exists()


def _age(path: Path, age_s: float) -> None:
    """Move `path`'s mtime `age_s` into the past."""
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def _unreadable(request: Path, *, age_s: float) -> None:
    """Turn one written envelope into the killed parent's unreadable remnant."""
    request.write_text("{not json")
    _age(request, age_s)


def _no_process_iteration(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
    """A `psutil.process_iter` stand-in yielding no processes at all."""
    return iter(())


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide machine processes so a classification test isolates its own legs.

    This box runs other agents' exec children; the widened unknown-timeout
    birth window would otherwise sweep a freshly started one and report LIVE.
    """
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _no_process_iteration)


def test_unreadable_envelope_past_the_bound_is_disposable_and_moves_with_a_receipt(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0-byte remnant older than the bound stops fencing recovery (D-2)."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    request.write_text("")  # the killed parent's zero-byte remnant
    _age(request, bound + 60)
    _no_processes(monkeypatch)

    (entry,) = survey(_AGENT, incumbent=None, resources=None)
    assert entry.verdict is Verdict.DISPOSABLE
    assert "unreadable" in entry.detail
    assert "bounded-disposition bound" in entry.detail

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    assert report.retained == ()
    (moved,) = report.quarantined
    assert not request.exists()
    assert moved.destination.read_bytes() == b""  # the bytes are preserved, never deleted
    receipt = json.loads((moved.destination.parent / "receipt.json").read_text())
    (record,) = receipt["entries"]
    assert record["verdict"] == "disposable"
    assert record["owner"] is None and record["generation"] is None
    assert record["live_pids"] == []


def test_unreadable_envelope_inside_the_bound_is_retained(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Young evidence still defers — a deferral that may settle is not a stall."""
    request = _write_envelope(exec_dir)
    _unreadable(request, age_s=60.0)
    _no_processes(monkeypatch)

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "not yet past the bounded-disposition bound" in entry.detail
    assert request.exists() and not quarantine_dir.exists()


def test_unreadable_envelope_with_a_live_host_is_retained(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored host identity that is not provably ended vetoes disposal."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    _unreadable(request, age_s=bound + 60)
    _no_processes(monkeypatch)
    native = psutil.Process()

    report = quarantine_stale(
        _AGENT,
        incumbent=None,
        resources=_resources(ResourceProcess.capture(native)),
        reason="unit test",
    )

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "not provably ended" in entry.detail
    assert request.exists()


def test_unreadable_envelope_with_a_live_reference_is_deferred(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """A live process naming the unreadable request keeps it deferred."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    _unreadable(request, age_s=bound + 60)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "AVA_EXEC_REQUEST_FILE": str(request)},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            shown = psutil.Process(child.pid).environ().get("AVA_EXEC_REQUEST_FILE")
            if shown == str(request):
                break
            time.sleep(0.05)
        report = quarantine_stale(
            _AGENT, incumbent=_superseded(), resources=None, reason="unit test"
        )
        (entry,) = report.retained
        assert entry.verdict is Verdict.LIVE
        assert child.pid in entry.live_pids
        assert request.exists()
    finally:
        # SIGKILL: SIGTERM=SIG_IGN may be inherited from a shell session.
        child.kill()
        child.wait(timeout=5)


def test_version_drift_envelope_is_still_retained_past_the_bound(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-2 bounds only unreadable bytes; every readable refusal keeps its contract."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    envelope = json.loads(request.read_text())
    envelope["v"] = 99
    request.write_text(json.dumps(envelope))
    _age(request, bound + 60)
    _no_processes(monkeypatch)

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "version" in entry.detail
    assert request.exists()


def test_unreadable_bounded_disposition_switch_off_retains(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AVA_EXEC_REQUEST_BOUNDED_QUARANTINE_ENABLED=false restores retention."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    _unreadable(request, age_s=bound + 60)
    _no_processes(monkeypatch)
    monkeypatch.setattr(exec_request_evidence, "_bounded_disposition_enabled", lambda: False)

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "unreadable" in entry.detail
    assert request.exists()


def test_bounded_age_tracks_the_registered_exec_node_timeout() -> None:
    """Where the profile keeps the sandbox domain, the bound is 2x the clock."""
    from shared.timing import EXEC_NODE_TIMEOUT_S

    assert exec_request_evidence._unreadable_expiry_age_s() == 2.0 * EXEC_NODE_TIMEOUT_S


def test_bounded_age_resolves_profile_safely_without_the_sandbox_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandbox-less process reads the cluster .env, then the declared default."""
    from shared import config as config_module
    from shared import runtime_config

    class _NoSandbox:
        def has_domain(self, _name: str) -> bool:
            return False

    monkeypatch.setattr(config_module, "settings", _NoSandbox())
    monkeypatch.setattr(
        runtime_config, "read_env_aliases", lambda: {"AVA_EXEC_NODE_TIMEOUT_SECONDS": "600"}
    )
    assert exec_request_evidence._exec_node_ceiling_s() == 600.0

    monkeypatch.setattr(runtime_config, "read_env_aliases", dict)
    assert exec_request_evidence._exec_node_ceiling_s() == 1200.0


def test_bounded_disposition_raises_the_counted_alert(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposing unreadable evidence without review is itself the alert."""
    from loguru import logger

    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    _unreadable(request, age_s=bound + 60)
    _no_processes(monkeypatch)
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="WARNING", format="{message}"
    )
    try:
        quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")
    finally:
        logger.remove(sink)

    alerts = [
        record
        for record in records
        if record["extra"].get("event") == "exec_request_bounded_quarantine"
    ]
    (alert,) = alerts
    extra = alert["extra"]
    assert extra["agent_id"] == _AGENT
    assert extra["reason"] == "unit test"
    assert extra["preserved"] == 1
    assert extra["sources"] == [str(request)]
    assert extra["bound_s"] == pytest.approx(exec_request_evidence._unreadable_expiry_age_s())


def test_routine_stale_quarantine_is_not_the_bounded_alert(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """Only the unreadable class escalates; attributed staleness stays quiet."""
    from loguru import logger

    _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="WARNING", format="{message}"
    )
    try:
        report = quarantine_stale(
            _AGENT, incumbent=_superseded(), resources=None, reason="unit test"
        )
    finally:
        logger.remove(sink)

    assert len(report.quarantined) == 1
    assert not [
        record
        for record in records
        if record["extra"].get("event") == "exec_request_bounded_quarantine"
    ]


def test_live_reference_of_any_process_shape_is_never_excluded(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """A live process naming the request keeps it deferred, whatever its argv."""
    request = _write_envelope(exec_dir, owner=uuid4())
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "AVA_EXEC_REQUEST_FILE": str(request)},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            shown = psutil.Process(child.pid).environ().get("AVA_EXEC_REQUEST_FILE")
            if shown == str(request):
                break
            time.sleep(0.05)
        report = quarantine_stale(
            _AGENT, incumbent=_superseded(), resources=None, reason="unit test"
        )
        (entry,) = report.retained
        assert entry.verdict is Verdict.LIVE
        # Machine noise (concurrent exec children) may add pids, never remove
        # this one: the live reference is what must keep the envelope deferred.
        assert child.pid in entry.live_pids
        assert request.exists()
    finally:
        # SIGKILL: SIGTERM=SIG_IGN is inherited from a shell session, so the
        # graceful call would leave the exec child alive.
        child.kill()
        child.wait(timeout=5)


def test_exec_child_with_hidden_environment_is_never_excluded(
    exec_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable is not absence: a hidden exec-child root still defers."""
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    hidden = Mock()
    hidden.info = {
        "pid": 987654,
        "status": psutil.STATUS_RUNNING,
        "cmdline": ["python", "-m", "agent.exec_child"],
    }
    hidden.environ.side_effect = psutil.AccessDenied()
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _only_process(hidden))

    (entry,) = survey(_AGENT, incumbent=_superseded(), resources=None)

    assert entry.verdict is Verdict.LIVE
    assert entry.live_pids == (987654,)
    assert request.exists()


@pytest.mark.parametrize(
    ("birth_offset", "verdict"),
    [(-3600.0, Verdict.STALE), (1.0, Verdict.LIVE)],
)
def test_exec_child_birth_window_bounds_a_scrubbed_root(
    exec_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    birth_offset: float,
    verdict: Verdict,
) -> None:
    """A readable non-matching exec child counts only inside the request's lifetime."""
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    scrubbed = Mock()
    scrubbed.info = {
        "pid": 987655,
        "status": psutil.STATUS_RUNNING,
        "cmdline": ["python", "-I", "-B", "-X", "utf8", "-m", "agent.exec_child"],
    }
    scrubbed.environ.return_value = {}
    scrubbed.create_time.return_value = request.stat().st_mtime + birth_offset
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _only_process(scrubbed))

    (entry,) = survey(_AGENT, incumbent=_superseded(), resources=None)

    assert entry.verdict is verdict


def test_live_stored_host_identity_retains_a_superseded_envelope(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    """Resource evidence that contradicts the boot premise vetoes every verdict."""
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    native = psutil.Process()

    report = quarantine_stale(
        _AGENT,
        incumbent=None,
        resources=_resources(ResourceProcess.capture(native)),
        reason="unit test",
    )

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "not provably ended" in entry.detail
    assert request.exists()


def test_reused_pid_is_the_ended_boot_not_a_live_host(exec_dir: Path, quarantine_dir: Path) -> None:
    """A recycled PID means the recorded host ended; the file may be quarantined."""
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    native = psutil.Process()
    current = ResourceProcess.capture(native)
    reused = current.model_copy(
        update={"starttime": current.starttime + 1}
        if current.starttime is not None
        else {"birth": current.birth - 100.0}
    )

    report = quarantine_stale(
        _AGENT,
        incumbent=None,
        resources=_resources(reused),
        reason="unit test",
    )

    assert report.retained == ()
    assert not request.exists()


def test_repeated_quarantine_is_idempotent(exec_dir: Path, quarantine_dir: Path) -> None:
    first = _write_envelope(exec_dir, "req-first.json", owner=uuid4(), age_s=3600)
    second = _write_envelope(exec_dir, "req-second.json", owner=uuid4(), age_s=3600)

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")
    assert len(report.quarantined) == 2 and report.event_dir is not None
    receipt = report.event_dir / str(_AGENT) / "receipt.json"
    before = receipt.read_text()
    assert not first.exists() and not second.exists()

    again = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    assert again.quarantined == () and again.retained == () and again.event_dir is None
    assert receipt.read_text() == before
    assert sorted(path.name for path in (receipt.parent).glob("req-*.json")) == [
        "req-first.json",
        "req-second.json",
    ]


def test_quarantine_move_failure_keeps_the_entry_retained(
    exec_dir: Path, quarantine_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    def refuse(_source: str, _destination: str) -> None:
        raise OSError("read-only quarantine")

    monkeypatch.setattr(exec_request_evidence.shutil, "move", refuse)

    report = quarantine_stale(_AGENT, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert "quarantine failed" in entry.detail
    assert request.exists()


def test_diagnostics_name_the_file_the_owner_and_the_commands(
    exec_dir: Path, quarantine_dir: Path
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    (entry,) = survey(_AGENT, incumbent=None, resources=None)
    line = entry.describe()
    hint = exec_request_evidence.disposition_hint(_AGENT)

    assert str(request) in line and "[stale]" in line and "live_pids=none" in line
    assert f"--agent {_AGENT}" in hint and "shared.exec_request_evidence" in hint


def test_cli_lists_and_quarantines_reviewed_entries(
    exec_dir: Path, quarantine_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    assert main(["--agent", str(_AGENT)]) == 0
    listing = capsys.readouterr().out
    assert "would quarantine" in listing and str(request) in listing

    assert main(["--agent", str(_AGENT), "--quarantine", request.name]) == 0
    assert "quarantined" in capsys.readouterr().out
    assert not request.exists()


def test_cli_refuses_an_unreviewed_retained_entry(
    exec_dir: Path, quarantine_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    assert main(["--agent", str(_AGENT), "--quarantine", request.name]) == 1
    assert "refusing" in capsys.readouterr().out
    assert request.exists()


def test_cli_quarantines_a_bounded_disposable_entry_without_force(
    exec_dir: Path,
    quarantine_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual path treats bounded-disposition evidence like stale evidence."""
    bound = exec_request_evidence._unreadable_expiry_age_s()
    request = _write_envelope(exec_dir, age_s=bound + 60)
    _unreadable(request, age_s=bound + 60)
    _no_processes(monkeypatch)

    assert main(["--agent", str(_AGENT)]) == 0
    listing = capsys.readouterr().out
    assert "would quarantine" in listing and "[disposable]" in listing

    assert main(["--agent", str(_AGENT), "--quarantine", request.name]) == 0
    assert "quarantined" in capsys.readouterr().out
    assert not request.exists()


def test_cli_force_still_quarantines_retained_evidence(
    exec_dir: Path, quarantine_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--force keeps its review override for everything retention still holds."""
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    assert main(["--agent", str(_AGENT), "--quarantine", request.name, "--force"]) == 0
    assert "quarantined" in capsys.readouterr().out
    assert not request.exists()
