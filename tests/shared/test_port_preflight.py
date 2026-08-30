"""Socket-table listener discovery used by local infrastructure probes."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import psutil
import pytest

from shared import port_preflight, proc


@dataclass(frozen=True)
class _Connection:
    status: str
    laddr: tuple[str, int] | tuple[()]


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

    monkeypatch.setattr(psutil, "net_connections", _net_connections)
    monkeypatch.setattr(proc, "run_bounded", _run)

    assert port_preflight.listener_addrs(6433) == {
        "10.0.0.72",
        "*",
        "127.0.0.1",
    }
    assert seen == [["lsof", "-nP", "-Fpn", "-sTCP:LISTEN", "-iTCP:6433"]]


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
