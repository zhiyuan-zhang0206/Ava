"""Managed E2E process diagnostics and stale-run residue tests (`tests/e2e/process_support.py`).

The reaper finds processes an e2e run left behind (identified by the
`ava_e2e_home_<pid>_<ts>` AVA_HOME most of them inherit, or, for the
session-scoped frontend whose env snapshot predates the e2e env layering, by
its `.builds/build-<pid>_<ts>` cwd) whose owning pytest process is gone, and
kills them. These tests cover the parsing and the live-run-protection
partitioning through the public support component. Native child proofs exercise
guarded signals without offering any unrelated host process to the sweep.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.e2e import process_support as processes


@pytest.mark.parametrize(
    ("command", "environment"),
    [
        (
            "uv run uvicorn gateway.app:app --port 60758",
            "AVA_HOME=/x/ava_e2e_home_1_2 AVA_DB_URL=postgresql://u@h/db",
        ),
        ("npm run start -p 60759", ""),
        ("next-server (v16.2.7)", "FOO=bar"),
    ],
)
def test_process_table_observation_retains_command_and_environment(
    command: str, environment: str
) -> None:
    observation = processes.ProcessObservation.from_ps_row(
        f"100 ?? S 0:00.01 {command} {environment}".rstrip()
    )
    assert observation == processes.ProcessObservation(100, command, environment)
    assert processes.ProcessObservation.from_ps_row("COMMAND PID USER") is None


@pytest.mark.parametrize(
    ("command", "environment", "cwd", "run"),
    [
        (
            "python server",
            "AVA_HOME=/r/tmp/ava_e2e_home_43948_1788018418678623",
            None,
            (43948, 1788018418678623),
        ),
        (
            "next-server (v16.2.7)",
            "",
            "/r/ui/web/.builds/build-43948_1788018418678623",
            (43948, 1788018418678623),
        ),
        ("npm run start", "", "/r/ui/web/.builds/build-1_2", (1, 2)),
        ("python server", "", "/r/ui/web/.builds/build-1_2", None),
        ("next-server", "", "/opt/other/.builds/build-1_2", None),
        ("next-server", "", "/r/ui/web/.builds/build-x_y", None),
        ("python server", "no marker here", None, None),
    ],
)
def test_e2e_process_classifies_observation(
    command: str, environment: str, cwd: str | None, run: tuple[int, int] | None
) -> None:
    process = processes.E2EProcess.from_observation(
        processes.ProcessObservation(100, command, environment), pgid=100, cwd=cwd
    )
    assert (process.run if process is not None else None) == run


def test_frontend_cwd_observation_keeps_path_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from subprocess import CompletedProcess

    cwd = "/Users/me/Ava with spaces/ui/web/.builds/build-1_2"

    def lsof(*args: object, **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            ["lsof"], 0, stdout=f"node 44147 user cwd DIR 1,15 64 836742558 {cwd}\n"
        )

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("subprocess.run", lsof)
    inspection = processes.ProcessInspection()
    assert inspection.working_directory(44147) == cwd
    process = processes.E2EProcess.from_observation(
        processes.ProcessObservation(44147, "next-server", ""),
        pgid=44147,
        cwd=inspection.working_directory(44147),
    )
    assert process is not None and process.run == (1, 2)


def _fake_ps_rows() -> list[tuple[int, str, str]]:
    return [
        (100, "uv run uvicorn gateway.app:app", "AVA_HOME=/r/tmp/ava_e2e_home_1_2"),
        (200, "next-server (v16.2.7)", ""),
        (300, "uv run uvicorn gateway.app:app", "AVA_HOME=/real/home"),
        (400, "python -m agent --agent-id 1003", "AVA_HOME=/r/tmp/ava_e2e_home_1_2"),
    ]


def _fake_cwd(pid: int) -> str | None:
    return "/r/ui/web/.builds/build-5_6" if pid == 200 else None


def _identity_pgid(pid: int) -> int:
    return pid


def test_scan_finds_env_marked_and_frontend_by_cwd() -> None:
    inspection = processes.ProcessInspection(
        rows=_fake_ps_rows, working_directory=_fake_cwd, group=_identity_pgid
    )
    procs = inspection.scan()
    assert [(p.pid, p.run) for p in procs] == [(100, (1, 2)), (200, (5, 6)), (400, (1, 2))]


def _dead_one_row() -> list[tuple[int, str, str]]:
    return [(100, "uv run uvicorn", "AVA_HOME=/r/ava_e2e_home_1_2")]


def _no_cwd(_pid: int) -> str | None:
    return None


def _dead_pgid(pid: int) -> int:
    raise ProcessLookupError


def test_scan_skips_process_that_died_mid_scan() -> None:
    inspection = processes.ProcessInspection(
        rows=_dead_one_row, working_directory=_no_cwd, group=_dead_pgid
    )
    assert inspection.scan() == []


def _live_owner_2(pid: int) -> bool:
    return pid == 2


def _no_alive(_pid: int) -> bool:
    return False


def _proc_for(pid: int, pgid: int, owner: int, cmdline: str = "uv") -> processes.E2EProcess:
    return processes.E2EProcess(pid=pid, pgid=pgid, cmdline=cmdline, run=(owner, 1))


def test_sweep_targets_kills_dead_owner_and_protects_live() -> None:
    procs = [
        _proc_for(10, 10, 1),  # group leader, dead owner -> group kill
        _proc_for(11, 10, 1),  # member of that group -> deduped into it
        _proc_for(20, 99, 1),  # non-leader, dead owner -> individual
        _proc_for(30, 30, 2, "npm"),  # leader, LIVE owner -> untouched
    ]
    plan = processes.ResidueSweepPlan.from_processes(
        procs,
        own_pid=99999,
        own_pgrp=88888,
        include_own=False,
        owner_live=_live_owner_2,
    )
    assert plan.groups == {10}
    assert plan.singles == {11, 20}
    assert plan.owners == {1}


def test_sweep_targets_include_own() -> None:
    procs = [
        _proc_for(10, 10, 99999),  # own run leader
        _proc_for(11, 11, 99999, "npm"),  # own run leader
        _proc_for(12, 12, 1),  # dead-run leader
    ]
    plan = processes.ResidueSweepPlan.from_processes(
        procs,
        own_pid=99999,
        own_pgrp=77777,
        include_own=True,
        owner_live=_no_alive,
    )
    assert plan.groups == {10, 11, 12}
    assert plan.singles == set()
    assert plan.owners == {1, 99999}


def test_sweep_targets_never_killpgs_own_pgrp() -> None:
    """A leader process whose pgid IS our own pgrp (a browser worker started
    by pytest in the same session) must go to singles, never to killpg."""
    procs = [_proc_for(10, 77777, 1, "chrome-headless-shell")]
    plan = processes.ResidueSweepPlan.from_processes(
        procs,
        own_pid=99999,
        own_pgrp=77777,
        include_own=False,
        owner_live=_no_alive,
    )
    assert plan.groups == set()
    assert plan.singles == {10}


def test_sweep_targets_skips_own_run_when_not_included() -> None:
    procs = [_proc_for(10, 10, 99999), _proc_for(11, 11, 1)]
    plan = processes.ResidueSweepPlan.from_processes(
        procs,
        own_pid=99999,
        own_pgrp=88888,
        include_own=False,
        owner_live=_no_alive,
    )
    assert plan.groups == {11}


def test_cwd_of_returns_str_or_none_for_real_process() -> None:
    """Platform ground truth, unmonkeypatched: `_cwd_of` must return
    `str | None` on every platform (re.Pattern.search rejects a Path). The
    Linux branch satisfies the contract via Path.readlink() — which returns a
    PosixPath, not a str — and the macOS branch via lsof output; the other
    tests monkeypatch `_cwd_of` and would mask exactly that regression (this
    one surfaced as CI e2e failures, not on the macOS dev box).
    """
    cwd = processes.ProcessInspection().working_directory(os.getpid())
    assert cwd is None or isinstance(cwd, str)


def _kill_ok(_pid: int, _sig: int) -> None:
    return None


def _cmd_pytest(_pid: int) -> str:
    return "/repo/.venv/bin/pytest tests/e2e/test_x.py"


def _cmd_xdist(_pid: int) -> str:
    return "python -u -c 'execnet... xdist worker'"


def _cmd_sshd(_pid: int) -> str:
    return "/usr/sbin/sshd"


def _cmd_none(_pid: int) -> str | None:
    return None


def _cmd_gateway(_pid: int) -> str:
    return "uv run uvicorn gateway.app:app --port 49397"


def _cmd_other(_pid: int) -> str:
    return "npm run start -p 60759"


@pytest.mark.parametrize(
    ("command", "live"),
    [(_cmd_pytest, True), (_cmd_xdist, True), (_cmd_sshd, False), (_cmd_none, True)],
)
def test_owner_liveness_preserves_an_unreadable_command(
    command: Callable[[int], str | None], live: bool
) -> None:
    inspection = processes.ProcessInspection(command=command, probe=_kill_ok)
    assert inspection.owner_live(123) is live


@pytest.mark.parametrize("owner_state", ["unknown", "live", "dead", "recycled"])
def test_native_sweep_preserves_an_unreadable_live_owner(tmp_path: Path, owner_state: str) -> None:
    """The real sweep sees only this test's birth-owned child, never host strangers."""
    owner_pid = 99999999
    with processes.managed_proc(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        label="owner-command-proof",
        log_path=str(tmp_path / "child.log"),
    ) as child:
        native = processes.ProcessInspection()
        command = native.command(child.pid)
        assert command is not None
        probes: list[tuple[int, int]] = []

        def rows() -> list[tuple[int, str, str]]:
            return [(child.pid, command, f"AVA_HOME=/proof/ava_e2e_home_{owner_pid}_1")]

        def probe(pid: int, sig: int) -> None:
            assert pid == owner_pid and sig == 0
            probes.append((pid, sig))
            if owner_state == "dead":
                raise ProcessLookupError

        def observed_command(pid: int) -> str | None:
            if pid == child.pid:
                return native.command(pid)
            assert pid == owner_pid
            assert owner_state != "dead"
            if owner_state == "unknown":
                return None
            return _cmd_pytest(pid) if owner_state == "live" else _cmd_sshd(pid)

        swept = processes.sweep_stale_e2e_processes(
            inspection=processes.ProcessInspection(rows=rows, command=observed_command, probe=probe)
        )
        assert probes == [(owner_pid, 0)]
        if owner_state in {"unknown", "live"}:
            assert swept == 0
            assert child.poll() is None
        else:
            assert swept == 1
            assert child.wait(timeout=5) != 0


def test_dead_owner_is_not_live() -> None:
    def gone(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    inspection = processes.ProcessInspection(command=_cmd_pytest, probe=gone)
    assert not inspection.owner_live(123)


@pytest.mark.parametrize(
    ("command", "observed", "matches"),
    [
        (_cmd_gateway, "uv run uvicorn gateway.app:app --port 49397", True),
        (_cmd_gateway, "uv  run uvicorn gateway.app:app --port 49397", True),
        (_cmd_other, "uv run uvicorn gateway.app:app", False),
        (_cmd_none, "x", False),
    ],
)
def test_identity_query_rechecks_observed_command(
    command: Callable[[int], str | None], observed: str, matches: bool
) -> None:
    inspection = processes.ProcessInspection(command=command)
    assert inspection.matches(_proc_for(100, 100, 1, observed)) is matches


def test_next_launch_preserves_failed_process_diagnostics(tmp_path: Path) -> None:
    log_path = tmp_path / "agent-host.log"
    records: list[str] = []
    for message, exit_code in (("first launch traceback", 7), ("next launch startup", 0)):
        command = [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1], "
            "file=sys.stderr if int(sys.argv[2]) else sys.stdout, flush=True); "
            "sys.exit(int(sys.argv[2]))",
            message,
            str(exit_code),
        ]
        with processes.managed_proc(
            command, label="log-retention-proof", log_path=str(log_path)
        ) as process:
            assert process.wait(timeout=5) == exit_code
            evidence = processes.dead_server_evidence()
            assert f"exit code {exit_code}" in evidence and message in evidence
        records.append(message)
        assert log_path.read_text().splitlines() == records
        assert processes.proc_log_tail(str(log_path)).splitlines() == records


def test_registered_server_query_exposes_only_the_active_fixture(tmp_path: Path) -> None:
    label = "registry-query-proof"
    with pytest.raises(KeyError):
        processes.registered_server(label)
    log_path = tmp_path / "fixture.log"
    with processes.managed_proc(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        label=label,
        log_path=str(log_path),
    ) as process:
        server = processes.registered_server(label)
        assert server.process is process and server.log_path == str(log_path)
        assert processes.dead_server_evidence() == ""
    assert process.returncode is not None
    with pytest.raises(KeyError):
        processes.registered_server(label)


def test_native_sweep_preserves_live_run_and_recycled_command(tmp_path: Path) -> None:
    """Only these three birth-owned fixture groups may be candidates in this proof."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        children = [
            stack.enter_context(
                processes.managed_proc(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    label=f"guard-proof-{index}",
                    log_path=str(tmp_path / f"child-{index}.log"),
                )
            )
            for index in range(3)
        ]
        native = processes.ProcessInspection()
        commands = [native.command(child.pid) for child in children]
        assert all(commands)

        def rows() -> list[tuple[int, str, str]]:
            return [
                (children[0].pid, str(commands[0]), "AVA_HOME=/proof/ava_e2e_home_99999999_1"),
                (
                    children[1].pid,
                    "foreign command replaced the scanned child",
                    "AVA_HOME=/proof/ava_e2e_home_99999999_1",
                ),
                (
                    children[2].pid,
                    str(commands[2]),
                    f"AVA_HOME=/proof/ava_e2e_home_{os.getpid()}_1",
                ),
            ]

        assert (
            processes.sweep_stale_e2e_processes(inspection=processes.ProcessInspection(rows=rows))
            == 2
        )  # Existing return contract counts planned targets, including guarded skips.
        assert children[0].wait(timeout=5) != 0
        assert children[1].poll() is None  # Command identity changed: never signalled.
        assert children[2].poll() is None  # This live concurrent run: never targeted.


def _ignore_cleanup_delay(_seconds: float) -> None:
    pass


def test_cleanup_receipt_records_decisions_and_guarded_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        (100, "fixture child", "AVA_HOME=/proof/ava_e2e_home_900_1"),
        (201, "fixture child", "AVA_HOME=/proof/ava_e2e_home_900_1"),
        (300, "fixture child", "AVA_HOME=/proof/ava_e2e_home_901_1"),
        (400, "fixture child", "AVA_HOME=/proof/ava_e2e_home_902_1"),
    ]
    commands: list[int] = []
    probes: list[int] = []
    signals: list[tuple[str, int, int]] = []

    def command(pid: int) -> str | None:
        commands.append(pid)
        return {901: "[pytest-xdist running] direct case", 902: None}.get(pid, "fixture child")

    def probe(pid: int, sig: int) -> None:
        assert sig == 0
        probes.append(pid)
        if pid == 900:
            raise ProcessLookupError

    def signal_group(pid: int, sig: int) -> None:
        signals.append(("group", pid, sig))

    def signal_single(pid: int, sig: int) -> None:
        signals.append(("single", pid, sig))

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(os, "kill", signal_single)
    monkeypatch.setattr("time.sleep", _ignore_cleanup_delay)
    path = tmp_path / "logs" / "cleanup.jsonl"
    inspection = processes.ProcessInspection(
        rows=lambda: rows,
        group=lambda pid: 100 if pid == 201 else pid,
        command=command,
        probe=probe,
    )
    assert processes.sweep_stale_e2e_processes(inspection=inspection, receipt_path=path) == 2
    assert probes == [900, 900, 901, 902]
    assert commands == [901, 902, 100, 201, 100, 201]
    assert signals == [
        (kind, pid, sig)
        for sig in (signal.SIGTERM, signal.SIGKILL)
        for kind, pid in (("group", 100), ("single", 201))
    ]
    _assert_sweep_receipt(path)


def _assert_sweep_receipt(path: Path) -> None:
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(row["sender"] == os.getpid() and row["time_ns"] > 0 for row in events)
    decisions = {row["target"]: row["selected"] for row in events if row["event"] == "decision"}
    assert decisions == {100: True, 201: True, 300: False, 400: False}
    owners = {row["owner"]: row for row in events if row["event"] == "owner"}
    assert owners[900]["probe"] == "gone" and not owners[900]["preserved"]
    assert owners[901]["hints"] == ["pytest", "xdist"] and owners[901]["preserved"]
    assert not owners[902]["command_known"] and owners[902]["preserved"]
    assert [row["outcome"] for row in events if row["event"] == "sweep_signal"] == [
        "attempt",
        "sent",
        "attempt",
        "sent",
        "attempt",
        "sent",
        "attempt",
        "sent",
    ]


@pytest.mark.parametrize("failure", [ProcessLookupError, PermissionError, RuntimeError])
def test_cleanup_receipt_keeps_signal_exception_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    def signal_failure(_pid: int, _sig: int) -> None:
        raise failure("original signal failure")

    monkeypatch.setattr(os, "killpg", signal_failure)
    monkeypatch.setattr("time.sleep", _ignore_cleanup_delay)
    inspection = processes.ProcessInspection(
        rows=lambda: [(100, "fixture child", "AVA_HOME=/proof/ava_e2e_home_900_1")],
        group=lambda pid: pid,
        command=lambda _pid: "fixture child",
        probe=_kill_ok,
    )
    path = tmp_path / "cleanup.jsonl"
    if failure is RuntimeError:
        with pytest.raises(RuntimeError, match="original signal failure"):
            processes.sweep_stale_e2e_processes(inspection=inspection, receipt_path=path)
    else:
        assert processes.sweep_stale_e2e_processes(inspection=inspection, receipt_path=path) == 1
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert [row["outcome"] for row in events if row["event"] == "sweep_signal"] == [
            "attempt",
            failure.__name__,
            "attempt",
            failure.__name__,
        ]


def test_unwritable_cleanup_receipt_keeps_the_original_query_result(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing file")
    inspection = processes.ProcessInspection(rows=list)
    assert (
        processes.sweep_stale_e2e_processes(
            inspection=inspection, receipt_path=blocked / "cleanup.jsonl"
        )
        == 0
    )


def test_cleanup_receipt_records_a_recycled_target_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_signal(_pid: int, _sig: int) -> None:
        pytest.fail("a recycled process must never be signalled")

    monkeypatch.setattr(os, "killpg", refuse_signal)
    monkeypatch.setattr("time.sleep", _ignore_cleanup_delay)
    inspection = processes.ProcessInspection(
        rows=lambda: [(100, "fixture child", "AVA_HOME=/proof/ava_e2e_home_900_1")],
        group=lambda pid: pid,
        command=lambda _pid: "replacement process",
        probe=_kill_ok,
    )
    path = tmp_path / "cleanup.jsonl"
    assert processes.sweep_stale_e2e_processes(inspection=inspection, receipt_path=path) == 1
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["outcome"] for row in events if row["event"] == "sweep_signal"] == [
        "identity_skip",
        "identity_skip",
    ]


def test_managed_frontend_receipt_identifies_its_exact_owner(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.jsonl"
    with processes.managed_proc(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        label="frontend-receipt-proof",
        receipt_path=path,
    ) as child:
        assert child.poll() is None
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["outcome"] for row in events] == ["attempt", "sent"]
    assert all(row["target"] == child.pid and row["sender"] == os.getpid() for row in events)
    assert events[0]["owner"] == os.getpid() and events[0]["signal"] == signal.SIGTERM


@pytest.mark.parametrize("serving", [False, True])
def test_public_module_entry_launches_with_only_the_explicit_fixture_gate(
    tmp_path: Path, serving: bool
) -> None:
    """Exercise the real -m entry used by gateway, agent-host, and ops fixtures."""
    gate = tmp_path / "serving"
    if serving:
        gate.write_text("ready\n")
    target = tmp_path / "fixture_gate_target.py"
    target.write_text(
        "from base.deploy.lifecycle import start_serving\n"
        "print(start_serving.is_serving(), flush=True)\n"
        "with start_serving.recovery_permitted() as allowed:\n"
        "    print(allowed, flush=True)\n"
    )
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(root)))
    log_path = tmp_path / "entry.log"
    with processes.managed_proc(
        [sys.executable, "-m", "tests.e2e.process_support", str(gate), target.stem],
        env=env,
        label="fixture-entry-proof",
        log_path=str(log_path),
    ) as child:
        assert child.wait(timeout=15) == 0
    assert log_path.read_text().splitlines() == [str(serving), str(serving)]
