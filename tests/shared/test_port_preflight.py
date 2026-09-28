"""Socket-table listener discovery used by local infrastructure probes."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

from shared import port_preflight, proc


@dataclass(frozen=True)
class _Connection:
    status: str
    laddr: tuple[str, int] | tuple[()]
    pid: int | None = None


def test_listener_addrs_reads_all_matching_psutil_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [
        _Connection("LISTEN", ("0.0.0.0", 6433)),  # noqa: S104 — socket-table wildcard fixture
        _Connection("LISTEN", ("::", 6433)),
        _Connection("LISTEN", ("2001:db8::5", 6433)),
        _Connection("LISTEN", ("127.0.0.1", 6434)),
        _Connection("ESTABLISHED", ("10.0.0.72", 6433)),
        _Connection("LISTEN", ()),
    ]

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return connections

    def _fail_lsof(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("psutil results must not fall back to lsof")

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _fail_lsof)

    assert port_preflight.listener_addrs(6433) == {
        "0.0.0.0",  # noqa: S104 — socket-table wildcard fixture
        "::",
        "2001:db8::5",
    }


def test_listener_addrs_falls_back_to_lsof_name_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        raise psutil.AccessDenied

    seen: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "p101\nn10.0.0.72:6433\n"
                "p102\nn*:6433\n"
                "p103\nn127.0.0.1:6433\n"
                "p104\nn192.0.2.1:6434\n"
            ),
            stderr="",
        )

    def _resolve(*args: str) -> list[str]:
        return ["/usr/sbin/lsof", *args]

    # A restricted PATH hides lsof, so resolution lands on the candidate list.
    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _run)
    monkeypatch.setattr(port_preflight, "_lsof_argv", _resolve)

    assert port_preflight.listener_addrs(6433) == {
        "10.0.0.72",
        "*",
        "127.0.0.1",
    }
    assert seen == [["/usr/sbin/lsof", "-nP", "-Fpn", "-sTCP:LISTEN", "-iTCP:6433"]]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("lsof unavailable"),
        subprocess.TimeoutExpired(["lsof"], 10),
    ],
)
def test_listener_addrs_returns_empty_when_lsof_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError | subprocess.TimeoutExpired,
) -> None:
    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        raise psutil.AccessDenied

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _run)

    assert port_preflight.listener_addrs(6433) == set()


def test_lsof_argv_prefers_path_then_absolute_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATH resolves first; a restricted PATH lands on the candidate list."""

    def _which(name: str) -> str | None:
        return "/opt/tools/lsof" if name == "lsof" else None

    monkeypatch.setattr(port_preflight.shutil, "which", _which)
    access_calls: list[tuple[str, int]] = []

    def _access(path: str, mode: int) -> bool:
        access_calls.append((path, mode))
        return False

    monkeypatch.setattr(port_preflight.os, "access", _access)
    assert port_preflight._lsof_argv("-nP") == ["/opt/tools/lsof", "-nP"]
    assert access_calls == []


def test_lsof_argv_scans_candidates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str) -> str | None:
        assert name == "lsof"
        return None

    monkeypatch.setattr(port_preflight.shutil, "which", _which)

    def _access(path: str, _mode: int) -> bool:
        return path == "/usr/bin/lsof"

    monkeypatch.setattr(port_preflight.os, "access", _access)
    assert port_preflight._lsof_argv("-nP") == ["/usr/bin/lsof", "-nP"]


def test_lsof_argv_none_when_lsof_is_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str) -> str | None:
        assert name == "lsof"
        return None

    def _access(path: str, mode: int) -> bool:
        assert path in port_preflight._LSOF_CANDIDATE_PATHS
        assert mode == os.X_OK
        return False

    monkeypatch.setattr(port_preflight.shutil, "which", _which)
    monkeypatch.setattr(port_preflight.os, "access", _access)
    assert port_preflight._lsof_argv("-nP") is None


def test_strict_listeners_returns_conclusive_psutil_without_lsof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [
        _Connection("LISTEN", ("0.0.0.0", 6433), pid=101),  # noqa: S104 — socket-table wildcard fixture
        _Connection("LISTEN", ("::", 6433), pid=101),
        _Connection("LISTEN", ("127.0.0.1", 6434), pid=102),
    ]

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return connections

    def _fail_lsof(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("a conclusive psutil scan must not fall back to lsof")

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _fail_lsof)

    assert port_preflight.strict_listeners_on(6433) == [101]


def test_strict_listeners_conclusive_absence_skips_lsof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table read that simply lacks the port proves absence — no lsof."""

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return [_Connection("LISTEN", ("127.0.0.1", 6434), pid=102)]

    def _fail_lsof(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("an absent port in a conclusive scan must not fall back to lsof")

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _fail_lsof)

    assert port_preflight.strict_listeners_on(6433) == []


def test_strict_listeners_falls_back_when_psutil_cannot_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LISTEN row with pid=None is inconclusive, not proof of absence."""
    connections = [
        _Connection("LISTEN", ("0.0.0.0", 6433), pid=None),  # noqa: S104 — socket-table wildcard fixture
        _Connection("LISTEN", ("127.0.0.1", 6434), pid=102),
    ]

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return connections

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "lsof"
        return subprocess.CompletedProcess(argv, 0, stdout="p101\np101\n", stderr="")

    def _resolve(*args: str) -> list[str]:
        return ["lsof", *args]

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _run)
    monkeypatch.setattr(port_preflight, "_lsof_argv", _resolve)

    assert port_preflight.strict_listeners_on(6433) == [101]


def test_strict_listeners_raises_when_sightings_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil saw a listener; lsof says no match. Two inspections disagree,
    so absence is not established — this must raise, not report empty."""
    connections = [_Connection("LISTEN", ("0.0.0.0", 6433), pid=None)]  # noqa: S104 — socket-table wildcard fixture

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return connections

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    def _resolve(*args: str) -> list[str]:
        return ["lsof", *args]

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _run)
    monkeypatch.setattr(port_preflight, "_lsof_argv", _resolve)

    with pytest.raises(port_preflight.ListenerDiscoveryError, match="neither psutil nor lsof"):
        port_preflight.strict_listeners_on(6433)


def test_strict_listeners_raises_when_lsof_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lsof anywhere means the inspection cannot run — honest failure."""
    connections = [_Connection("LISTEN", ("0.0.0.0", 6433), pid=None)]  # noqa: S104 — socket-table wildcard fixture

    def _net_connections(*, kind: str) -> list[_Connection]:
        assert kind == "tcp"
        return connections

    def _fail_lsof(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("unreachable lsof must not be executed")

    def _resolve(*args: str) -> list[str] | None:
        assert args
        return None

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _fail_lsof)
    monkeypatch.setattr(port_preflight, "_lsof_argv", _resolve)

    with pytest.raises(port_preflight.ListenerDiscoveryError, match="lsof is not on PATH"):
        port_preflight.strict_listeners_on(6433)


def test_process_mentions_reads_the_cwd_when_argv_carries_no_marker(tmp_path: Path) -> None:
    """A service launched with a relative argv is still attributed through its cwd;
    a marker none of argv/exe/cwd carries, and a gone pid, are never ours."""
    unit = tmp_path / "unit"
    unit.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=unit)
    try:
        assert port_preflight.process_mentions(child.pid, (str(unit.resolve()),))
        assert not port_preflight.process_mentions(child.pid, (str(tmp_path / "foreign"),))
    finally:
        child.kill()
        child.wait(timeout=5)
    assert not port_preflight.process_mentions(child.pid, (str(unit.resolve()),))


@pytest.mark.parametrize(
    ("owners", "expected"),
    [
        ((), False),
        ((101, 102), True),
        ((101, 103), False),
    ],
)
def test_listener_is_ours_requires_every_listener_to_mention_a_marker(
    monkeypatch: pytest.MonkeyPatch, owners: tuple[int, ...], expected: bool
) -> None:
    """No listener is not ours, and one foreign listener makes the port foreign."""
    ours = {101, 102}

    def _listeners_on(port: int) -> list[int]:
        assert port == 6433
        return list(owners)

    def _process_mentions(pid: int, markers: tuple[str, ...]) -> bool:
        assert markers == ("/unit",)
        return pid in ours

    monkeypatch.setattr(port_preflight, "listeners_on", _listeners_on)
    monkeypatch.setattr(port_preflight, "process_mentions", _process_mentions)

    assert port_preflight.listener_is_ours(6433, ("/unit",)) is expected
