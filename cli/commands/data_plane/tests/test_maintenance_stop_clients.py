"""The data-plane stop names the clients still on the pooler before it stops it — and only
reports: nothing here changes the stop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from base.cluster.dataplane import pooler as pooler_files
from cli.commands.data_plane import maintenance_stop as plane

_ADMIN = "admin-secret"


def _client(n: int) -> pooler_files.PoolerClient:
    return pooler_files.PoolerClient(
        address=f"10.1.2.{n}:5432",
        database="ava",
        user="ava_runner",
        state="idle",
        application="",
    )


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> list[pooler_files.PoolerClient]:
    """A readable console listing whatever the test appends."""
    listed: list[pooler_files.PoolerClient] = []
    monkeypatch.setattr(plane.OwnedPooler, "from_config", lambda *_a: SimpleNamespace(port=6432))
    monkeypatch.setattr(plane, "read_pooler_admin", lambda _home: SimpleNamespace(password=_ADMIN))
    monkeypatch.setattr(pooler_files, "clients", lambda _port, _password: list(listed))
    return listed


def test_remaining_clients_are_named_in_stderr_and_the_report(
    console: list[pooler_files.PoolerClient], capsys: pytest.CaptureFixture[str]
) -> None:
    console.extend([_client(7), _client(8)])
    report: list[str] = []

    plane._report_pooler_clients(SimpleNamespace(), report)  # pyright: ignore[reportArgumentType]

    err = capsys.readouterr().err
    assert "2 client(s) still connected when the pooler stops" in err
    assert "10.1.2.7:5432 db=ava user=ava_runner state=idle" in err
    assert report == [err.strip().removeprefix("! ")]


def test_a_long_list_is_cut_with_a_count(
    console: list[pooler_files.PoolerClient], capsys: pytest.CaptureFixture[str]
) -> None:
    console.extend(_client(n) for n in range(25))

    plane._report_pooler_clients(SimpleNamespace(), None)  # pyright: ignore[reportArgumentType]

    err = capsys.readouterr().err
    assert "25 client(s)" in err and "; +15 more" in err
    assert "10.1.2.9:" in err and "10.1.2.10:" not in err


def test_no_clients_says_nothing(
    console: list[pooler_files.PoolerClient], capsys: pytest.CaptureFixture[str]
) -> None:
    report: list[str] = []

    plane._report_pooler_clients(SimpleNamespace(), report)  # pyright: ignore[reportArgumentType]

    assert capsys.readouterr().err == "" and report == []


def test_an_unreadable_console_is_reported_as_unreadable_not_as_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(plane.OwnedPooler, "from_config", lambda *_a: SimpleNamespace(port=6432))

    def unreadable(_home: object) -> object:
        raise FileNotFoundError("no admin secret")

    monkeypatch.setattr(plane, "read_pooler_admin", unreadable)
    report: list[str] = []

    plane._report_pooler_clients(SimpleNamespace(), report)  # pyright: ignore[reportArgumentType]

    assert "could not be listed before the pooler stop" in capsys.readouterr().err
    assert len(report) == 1 and "no admin secret" in report[0]
