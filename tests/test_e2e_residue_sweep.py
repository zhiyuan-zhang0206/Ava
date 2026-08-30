"""Unit tests for the e2e stale-run residue reaper (`tests/e2e/_proc.py`).

The reaper finds processes an e2e run left behind (identified by the
`ava_e2e_home_<pid>_<ts>` AVA_HOME most of them inherit, or, for the
session-scoped frontend whose env snapshot predates the e2e env layering, by
its `.builds/build-<pid>_<ts>` cwd) whose owning pytest process is gone, and
kills them. These tests cover the parsing and the live-run-protection
partitioning — the kill loop itself is exercised by the e2e package's two
sweep calls (a signal to nothing is not a unit-testable contract).
"""

from __future__ import annotations

import os

import pytest

from tests.e2e import _proc


def test_split_cmdline_env() -> None:
    cmdline, env = _proc._split_cmdline_env(
        "uv run uvicorn gateway.app:app --port 60758 AVA_HOME=/x/ava_e2e_home_1_2"
        " AVA_DB_URL=postgresql://u@h/db"
    )
    assert cmdline == "uv run uvicorn gateway.app:app --port 60758"
    assert env == "AVA_HOME=/x/ava_e2e_home_1_2 AVA_DB_URL=postgresql://u@h/db"


def test_split_cmdline_env_without_env() -> None:
    cmdline, env = _proc._split_cmdline_env("npm run start -p 60759")
    assert cmdline == "npm run start -p 60759"
    assert env == ""


def test_split_cmdline_env_arg_shaped_like_env_is_argv() -> None:
    """An argv token that merely *looks* like an env assignment is still argv:
    the split is at the FIRST env token, which is the boundary between the
    command and the env block in `ps eww` output."""
    cmdline, env = _proc._split_cmdline_env("next-server (v16.2.7) FOO=bar")
    assert cmdline == "next-server (v16.2.7)"
    assert env == "FOO=bar"


def test_parse_run_id_home_and_build() -> None:
    assert _proc._parse_run_id(
        "AVA_HOME=/r/tmp/ava_e2e_home_43948_1788018418678623", _proc._E2E_HOME_RUN_RE
    ) == (43948, 1788018418678623)
    assert _proc._parse_run_id(
        "/r/ui/web/.builds/build-43948_1788018418678623", _proc._E2E_BUILD_RUN_RE
    ) == (43948, 1788018418678623)
    assert _proc._parse_run_id("no marker here", _proc._E2E_HOME_RUN_RE) is None
    assert _proc._parse_run_id("/r/ui/web/.builds/build-x_y", _proc._E2E_BUILD_RUN_RE) is None


def test_parse_lsof_cwd_keeps_path_with_spaces() -> None:
    out = (
        "node  44147 zyonzhang  cwd    DIR   1,15       64 836742558 "
        "/Users/me/Ava/.worktrees/x/ui/web/.builds/build-1_2\n"
    )
    assert _proc._parse_lsof_cwd(out) == "/Users/me/Ava/.worktrees/x/ui/web/.builds/build-1_2"
    assert _proc._parse_lsof_cwd("COMMAND PID USER FD TYPE DEVICE NODE NAME\n") is None


def test_looks_like_frontend() -> None:
    assert _proc._looks_like_frontend("next-server (v16.2.7)")
    assert _proc._looks_like_frontend("npm run start -p 60759")
    assert not _proc._looks_like_frontend("uv run uvicorn gateway.app:app")


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


def test_scan_finds_env_marked_and_frontend_by_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_proc, "_ps_rows_with_env", _fake_ps_rows)
    monkeypatch.setattr(_proc, "_cwd_of", _fake_cwd)
    monkeypatch.setattr(os, "getpgid", _identity_pgid)

    procs = _proc.scan_e2e_processes()
    assert [(p.pid, p.run) for p in procs] == [(100, (1, 2)), (200, (5, 6)), (400, (1, 2))]


def _dead_one_row() -> list[tuple[int, str, str]]:
    return [(100, "uv run uvicorn", "AVA_HOME=/r/ava_e2e_home_1_2")]


def _no_cwd(_pid: int) -> str | None:
    return None


def _dead_pgid(pid: int) -> int:
    raise ProcessLookupError


def test_scan_skips_process_that_died_mid_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_proc, "_ps_rows_with_env", _dead_one_row)
    monkeypatch.setattr(_proc, "_cwd_of", _no_cwd)
    monkeypatch.setattr(os, "getpgid", _dead_pgid)
    assert _proc.scan_e2e_processes() == []


def _live_owner_2(pid: int) -> bool:
    return pid == 2


def _no_alive(_pid: int) -> bool:
    return False


def _proc_for(pid: int, pgid: int, owner: int, cmdline: str = "uv") -> _proc.E2EProcess:
    return _proc.E2EProcess(pid=pid, pgid=pgid, cmdline=cmdline, run=(owner, 1))


def test_sweep_targets_kills_dead_owner_and_protects_live() -> None:
    procs = [
        _proc_for(10, 10, 1),  # group leader, dead owner -> group kill
        _proc_for(11, 10, 1),  # member of that group -> deduped into it
        _proc_for(20, 99, 1),  # non-leader, dead owner -> individual
        _proc_for(30, 30, 2, "npm"),  # leader, LIVE owner -> untouched
    ]
    groups, singles, owners = _proc._sweep_targets(
        procs,
        own_pid=99999,
        own_pgrp=88888,
        include_own=False,
        owner_live=_live_owner_2,
    )
    assert groups == {10}
    assert singles == {11, 20}
    assert owners == {1}


def test_sweep_targets_include_own() -> None:
    procs = [
        _proc_for(10, 10, 99999),  # own run leader
        _proc_for(11, 11, 99999, "npm"),  # own run leader
        _proc_for(12, 12, 1),  # dead-run leader
    ]
    groups, singles, owners = _proc._sweep_targets(
        procs,
        own_pid=99999,
        own_pgrp=77777,
        include_own=True,
        owner_live=_no_alive,
    )
    assert groups == {10, 11, 12}
    assert singles == set()
    assert owners == {1, 99999}


def test_sweep_targets_never_killpgs_own_pgrp() -> None:
    """A leader process whose pgid IS our own pgrp (a browser worker started
    by pytest in the same session) must go to singles, never to killpg."""
    procs = [_proc_for(10, 77777, 1, "chrome-headless-shell")]
    groups, singles, _owners = _proc._sweep_targets(
        procs,
        own_pid=99999,
        own_pgrp=77777,
        include_own=False,
        owner_live=_no_alive,
    )
    assert groups == set()
    assert singles == {10}


def test_sweep_targets_skips_own_run_when_not_included() -> None:
    procs = [_proc_for(10, 10, 99999), _proc_for(11, 11, 1)]
    groups, _singles, _owners = _proc._sweep_targets(
        procs,
        own_pid=99999,
        own_pgrp=88888,
        include_own=False,
        owner_live=_no_alive,
    )
    assert groups == {11}


def test_cwd_of_returns_str_or_none_for_real_process() -> None:
    """Platform ground truth, unmonkeypatched: `_cwd_of` must return
    `str | None` on every platform (re.Pattern.search rejects a Path). The
    Linux branch satisfies the contract via Path.readlink() — which returns a
    PosixPath, not a str — and the macOS branch via lsof output; the other
    tests monkeypatch `_cwd_of` and would mask exactly that regression (this
    one surfaced as CI e2e failures, not on the macOS dev box).
    """
    cwd = _proc._cwd_of(os.getpid())
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


def test_parse_run_id_build_pattern_requires_ui_web_dir() -> None:
    """The frontend marker is anchored to the repo's build dir — a foreign
    `.builds/build-<pid>_<ts>` directory must not make a process e2e-owned."""
    assert _proc._parse_run_id("/opt/other/.builds/build-1_2", _proc._E2E_BUILD_RUN_RE) is None
    assert _proc._parse_run_id("/r/ui/web/.builds/build-1_2", _proc._E2E_BUILD_RUN_RE) == (1, 2)


def test_owner_live_requires_a_pytest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recycled pid must not hold the residue hostage: `os.kill(pid, 0)`
    succeeding on an unrelated process is not a live run."""
    monkeypatch.setattr(os, "kill", _kill_ok)

    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_pytest)
    assert _proc._owner_live(123) is True
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_xdist)
    assert _proc._owner_live(123) is True
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_sshd)
    assert _proc._owner_live(123) is False
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_none)
    assert _proc._owner_live(123) is False

    def _gone(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _gone)
    assert _proc._owner_live(123) is False


def test_identity_holds_compares_command_before_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOCTOU guard: the process at a pid is only signalled while it is still
    the exact process the scan matched (whitespace-tolerant compare)."""
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_gateway)
    assert _proc._identity_holds(100, "uv run uvicorn gateway.app:app --port 49397") is True
    assert _proc._identity_holds(100, "uv  run uvicorn gateway.app:app --port 49397") is True
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_other)
    assert _proc._identity_holds(100, "uv run uvicorn gateway.app:app") is False
    monkeypatch.setattr(_proc, "_ps_command_of", _cmd_none)
    assert _proc._identity_holds(100, "x") is False
