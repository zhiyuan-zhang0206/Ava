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

from base.agents.incarnation import exec_request_evidence
from base.agents.incarnation.exec_request_evidence import (
    Verdict,
    main,
    quarantine_stale,
    survey,
)
from base.agents.incarnation.resources import IncarnationResources, ResourceProcess
from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import exec_run_dir, quarantined_exec_requests_dir

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

    def iter_processes(_attrs: list[str]) -> Iterator[Mock]:
        return iter([process])

    return iter_processes


def _resources(host: ResourceProcess) -> dict[str, object]:
    return IncarnationResources(
        generation=uuid4(), owner=uuid4(), host_process=host, requests={}
    ).model_dump(mode="json")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def exec_dir(home: Path) -> Path:
    directory = exec_run_dir(home)
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def quarantine_dir(home: Path) -> Path:
    return quarantined_exec_requests_dir(home)


def test_superseded_envelope_is_stale_and_quarantined_with_receipt(
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    owner = uuid4()
    request = _write_envelope(exec_dir, owner=owner, age_s=3600)
    before = request.read_text()
    incumbent = _superseded()

    (entry,) = survey(_AGENT, home=home, incumbent=incumbent, resources=None)
    assert entry.verdict is Verdict.STALE
    assert entry.incarnation is not None and entry.incarnation.owner == owner
    assert entry.live_pids == ()

    report = quarantine_stale(
        _AGENT, home=home, incumbent=incumbent, resources=None, reason="unit test"
    )
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
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    report = quarantine_stale(_AGENT, home=home, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "no incarnation" in entry.detail
    assert request.exists() and not quarantine_dir.exists()


def _age(path: Path, age_s: float) -> None:
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def _unreadable(request: Path, *, age_s: float) -> None:
    request.write_text("{not json")
    _age(request, age_s)


def _no_process_iteration(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
    return iter(())


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _no_process_iteration)


def test_unreadable_envelope_inside_the_bound_is_retained(
    exec_dir: Path,
    quarantine_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir)
    _unreadable(request, age_s=60.0)
    _no_processes(monkeypatch)

    report = quarantine_stale(_AGENT, home=home, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "unreadable" in entry.detail
    assert request.exists() and not quarantine_dir.exists()


def test_bounded_age_resolves_profile_safely_without_the_sandbox_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base import config as config_module
    from base.host.env import dotenv_file

    class _NoSandbox:
        def has_domain(self, _name: str) -> bool:
            return False

    monkeypatch.setattr(config_module, "settings", _NoSandbox())
    monkeypatch.setattr(
        dotenv_file, "read_env_aliases", lambda: {"MEIVA_EXEC_NODE_TIMEOUT_SECONDS": "600"}
    )
    assert exec_request_evidence.exec_node_ceiling_s() == 600.0

    monkeypatch.setattr(dotenv_file, "read_env_aliases", dict)
    assert exec_request_evidence.exec_node_ceiling_s() == 1200.0


def test_routine_stale_quarantine_is_not_the_bounded_alert(
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    from loguru import logger

    _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="WARNING", format="{message}"
    )
    try:
        report = quarantine_stale(
            _AGENT, home=home, incumbent=_superseded(), resources=None, reason="unit test"
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
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4())
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "MEIVA_EXEC_REQUEST_FILE": str(request)},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            shown = psutil.Process(child.pid).environ().get("MEIVA_EXEC_REQUEST_FILE")
            if shown == str(request):
                break
            time.sleep(0.05)
        report = quarantine_stale(
            _AGENT, home=home, incumbent=_superseded(), resources=None, reason="unit test"
        )
        (entry,) = report.retained
        assert entry.verdict is Verdict.LIVE
        assert child.pid in entry.live_pids
        assert request.exists()
    finally:
        child.kill()
        child.wait(timeout=5)


def test_exec_child_with_hidden_environment_is_never_excluded(
    exec_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    hidden = Mock()
    hidden.info = {
        "pid": 987654,
        "status": psutil.STATUS_RUNNING,
        "cmdline": ["python", "-m", "agent.execution.child"],
    }
    hidden.environ.side_effect = psutil.AccessDenied()
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _only_process(hidden))

    (entry,) = survey(_AGENT, home=home, incumbent=_superseded(), resources=None)

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
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    scrubbed = Mock()
    scrubbed.info = {
        "pid": 987655,
        "status": psutil.STATUS_RUNNING,
        "cmdline": ["python", "-I", "-B", "-X", "utf8", "-m", "agent.execution.child"],
    }
    scrubbed.environ.return_value = {}
    scrubbed.create_time.return_value = request.stat().st_mtime + birth_offset
    monkeypatch.setattr(exec_request_evidence.psutil, "process_iter", _only_process(scrubbed))

    (entry,) = survey(_AGENT, home=home, incumbent=_superseded(), resources=None)

    assert entry.verdict is verdict


def test_live_stored_host_identity_retains_a_superseded_envelope(
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)
    native = psutil.Process()

    report = quarantine_stale(
        _AGENT,
        home=home,
        incumbent=None,
        resources=_resources(ResourceProcess.capture(native)),
        reason="unit test",
    )

    (entry,) = report.retained
    assert entry.verdict is Verdict.UNKNOWN
    assert "not provably ended" in entry.detail
    assert request.exists()


def test_reused_pid_is_the_ended_boot_not_a_live_host(
    exec_dir: Path, quarantine_dir: Path, home: Path
) -> None:
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
        home=home,
        incumbent=None,
        resources=_resources(reused),
        reason="unit test",
    )

    assert report.retained == ()
    assert not request.exists()


def test_repeated_quarantine_is_idempotent(
    exec_dir: Path, quarantine_dir: Path, home: Path
) -> None:
    first = _write_envelope(exec_dir, "req-first.json", owner=uuid4(), age_s=3600)
    second = _write_envelope(exec_dir, "req-second.json", owner=uuid4(), age_s=3600)

    report = quarantine_stale(_AGENT, home=home, incumbent=None, resources=None, reason="unit test")
    assert len(report.quarantined) == 2 and report.event_dir is not None
    receipt = report.event_dir / str(_AGENT) / "receipt.json"
    before = receipt.read_text()
    assert not first.exists() and not second.exists()

    again = quarantine_stale(_AGENT, home=home, incumbent=None, resources=None, reason="unit test")

    assert again.quarantined == () and again.retained == () and again.event_dir is None
    assert receipt.read_text() == before
    assert sorted(path.name for path in (receipt.parent).glob("req-*.json")) == [
        "req-first.json",
        "req-second.json",
    ]


def test_quarantine_move_failure_keeps_the_entry_retained(
    exec_dir: Path,
    quarantine_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    def refuse(_source: str, _destination: str) -> None:
        raise OSError("read-only quarantine")

    monkeypatch.setattr(exec_request_evidence.shutil, "move", refuse)

    report = quarantine_stale(_AGENT, home=home, incumbent=None, resources=None, reason="unit test")

    (entry,) = report.retained
    assert "quarantine failed" in entry.detail
    assert request.exists()


def test_diagnostics_name_the_file_the_owner_and_the_commands(
    exec_dir: Path,
    quarantine_dir: Path,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    (entry,) = survey(_AGENT, home=home, incumbent=None, resources=None)
    line = entry.describe()
    hint = exec_request_evidence.disposition_hint(_AGENT)

    assert str(request) in line and "[stale]" in line and "live_pids=none" in line
    assert f"--agent {_AGENT}" in hint and "base.agents.incarnation.exec_request_evidence" in hint


def test_cli_lists_and_quarantines_reviewed_entries(
    db_url: str,
    exec_dir: Path,
    quarantine_dir: Path,
    capsys: pytest.CaptureFixture[str],
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, owner=uuid4(), age_s=3600)

    assert main(["--agent", str(_AGENT)], home=home) == 0
    listing = capsys.readouterr().out
    assert "would quarantine" in listing and str(request) in listing

    assert main(["--agent", str(_AGENT), "--quarantine", request.name], home=home) == 0
    assert "quarantined" in capsys.readouterr().out
    assert not request.exists()


def test_cli_refuses_an_unreviewed_retained_entry(
    db_url: str,
    exec_dir: Path,
    quarantine_dir: Path,
    capsys: pytest.CaptureFixture[str],
    home: Path,
) -> None:
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    assert main(["--agent", str(_AGENT), "--quarantine", request.name], home=home) == 1
    assert "refusing" in capsys.readouterr().out
    assert request.exists()


def test_cli_force_still_quarantines_retained_evidence(
    db_url: str,
    exec_dir: Path,
    quarantine_dir: Path,
    capsys: pytest.CaptureFixture[str],
    home: Path,
) -> None:
    request = _write_envelope(
        exec_dir, payload={"v": 1, "code": "x", "agent_id": _AGENT, "timeout_s": 5.0}, age_s=3600
    )

    assert main(["--agent", str(_AGENT), "--quarantine", request.name, "--force"], home=home) == 0
    assert "quarantined" in capsys.readouterr().out
    assert not request.exists()


def test_cli_entry_captures_once_and_keeps_the_database_gated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    home: Path,
) -> None:
    image = LoadedCommit(source_root=tmp_path, sha="loaded-before-checkout-moved")
    capture = Mock(return_value=image)
    count = Mock(return_value=7)
    database = Mock()
    create = Mock(return_value=database)
    identity = Mock(return_value=(None, None))
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(exec_request_evidence, "process_name", lambda: "diagnostic")
    monkeypatch.setattr(exec_request_evidence.Database, "from_settings", create)
    monkeypatch.setattr(exec_request_evidence, "row_identity", identity)
    monkeypatch.setattr(exec_request_evidence, "survey", Mock(return_value=[]))
    monkeypatch.setattr(exec_request_evidence, "print_request_evidence", Mock())

    assert main(["--agent", str(_AGENT)], home=home) == 0

    capture.assert_called_once_with()
    create.assert_called_once()
    identity.assert_called_once_with(database, _AGENT)
    gate = create.call_args.kwargs["gate"]
    assert isinstance(gate, ProcessDbGate)
    count.assert_not_called()
    assert gate.application_name() == "meiva:diagnostic:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    gate.observe_minimum(7)
    assert not gate.min_read_due()


def test_cli_entry_propagates_the_original_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    failure = RuntimeError("original database failure")
    monkeypatch.setattr(exec_request_evidence.Database, "from_settings", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        main(["--agent", str(_AGENT)], home=home)
    assert caught.value is failure


def test_old_unreadable_request_remains_unknown_without_review(
    exec_dir: Path,
    quarantine_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    request = _write_envelope(exec_dir, age_s=1000000)
    _unreadable(request, age_s=1000000)
    _no_processes(monkeypatch)
    report = quarantine_stale(
        _AGENT, home=home, incumbent=None, resources=None, reason="inspection"
    )
    assert report.quarantined == ()
    assert len(report.retained) == 1
    assert report.retained[0].verdict is Verdict.UNKNOWN
    assert request.exists()
