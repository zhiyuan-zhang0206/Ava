"""`ava cluster update --target` — per-host trigger via the gateway relay."""

from __future__ import annotations

import argparse
from typing import Any, cast

import httpx
import pytest

from cli.parsers import cluster as _cluster_parser


class _Resp:
    def __init__(self, status_code: int, body: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body: dict[str, str] = body or {}

    def json(self) -> dict[str, str]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://gw:8000/api/cluster/update")
            raise httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(self.status_code, request=req)
            )


def _gw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")


def test_target_posts_relay_and_prints_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gw(monkeypatch)
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        return _Resp(202, {"session": "ava-updater-1", "log": "upd.log"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    rc = cmd_update(target="macmini", target_sha="abc123")
    assert rc == 0
    out = capsys.readouterr().out
    assert seen["url"] == "http://gw:8000/api/cluster/update"
    assert seen["params"] == {"target": "macmini", "target_sha": "abc123"}
    assert "session=ava-updater-1" in out
    assert "pinned: abc123" in out


def test_target_without_sha_omits_the_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gw(monkeypatch)
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        seen["params"] = kwargs.get("params")
        return _Resp(202, {"session": "s", "log": "l"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update(target="company-mini") == 0
    assert seen["params"] == {"target": "company-mini"}


@pytest.mark.parametrize(
    "extra",
    [
        {"local": True},
        {"restart_only": True},
        {"force": True},
        {"dry_run": True},
        {"mode": "force"},
    ],
)
def test_target_refuses_whole_cluster_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], extra: dict[str, object]
) -> None:
    _gw(monkeypatch)

    def fail_post(*_a: object, **_k: object) -> _Resp:
        raise AssertionError("no POST may be attempted for a refused combination")

    monkeypatch.setattr("httpx.post", fail_post)

    from cli.commands import cmd_update

    rc = cmd_update(target="macmini", **cast("dict[str, Any]", extra))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--target" in err
    assert "cannot be combined" in err


def test_target_sha_requires_target(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.commands import cmd_update

    assert cmd_update(target_sha="abc123") == 2
    assert "--target-sha requires --target" in capsys.readouterr().err


def test_target_503_surfaces_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gw(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> _Resp:
        return _Resp(503, {"detail": "machine 'macmini' ops server unreachable"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update(target="macmini") == 1
    assert "ops server unreachable" in capsys.readouterr().err


def test_target_502_surfaces_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gw(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> _Resp:
        return _Resp(502, {"detail": "machine 'macmini' cluster_update failed"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update(target="macmini") == 1
    assert "cluster_update failed" in capsys.readouterr().err


def test_target_origin_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gw(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> _Resp:
        return _Resp(202, {"session": "s", "log": "l"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update(target="macmini", origin="agent:9") == 0
    assert "ignored" in capsys.readouterr().err


def test_parser_wires_target_flags() -> None:
    from cli.main import _build_parser

    ns = _build_parser().parse_args(
        ["cluster", "update", "--target", "macmini", "--target-sha", "abc123"]
    )
    assert ns.target == "macmini"
    assert ns.target_sha == "abc123"
    plain = _build_parser().parse_args(["cluster", "update"])
    assert plain.target is None
    assert plain.target_sha is None


def test_cluster_parser_directly_registers_target_flags() -> None:
    """The parser layer carries the flags on its own.

    This test imports `cli.parsers.cluster` directly (not only through
    `cli.main`) so the PR test selector maps parser changes to this test via a
    real direct-import edge instead of the blind-file full-suite fallback.
    """
    parser = argparse.ArgumentParser()
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers()
    _cluster_parser._add_cluster_parser(subparsers)
    args = parser.parse_args(["cluster", "update", "--target", "macmini", "--target-sha", "abc"])
    assert args.target == "macmini"
    assert args.target_sha == "abc"
