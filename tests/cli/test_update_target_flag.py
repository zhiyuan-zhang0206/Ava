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


def test_target_with_default_mode_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--mode smooth` is the default value; it cannot be told apart from an
    explicit one, so the boundary of the refusal is `--mode force` only."""
    _gw(monkeypatch)

    def fake_post(url: str, **kwargs: object) -> _Resp:
        return _Resp(202, {"session": "s", "log": "l"})

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update(target="macmini", mode="smooth") == 0


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

    # Full 40-hex; abbreviated targets are refused at parse (issue #2343).
    target_sha = "abc123" + "0" * 34
    ns = _build_parser().parse_args(
        ["cluster", "update", "--target", "macmini", "--target-sha", target_sha]
    )
    assert ns.target == "macmini"
    assert ns.target_sha == target_sha
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
    target_sha = "abc" + "0" * 37  # full 40-hex; abbreviated targets are refused at parse (#2343)
    args = parser.parse_args(
        ["cluster", "update", "--target", "macmini", "--target-sha", target_sha]
    )
    assert args.target == "macmini"
    assert args.target_sha == target_sha


# ── parse-layer gates (task #4092, batch B4) ──


def test_parse_gate_refuses_target_combined_with_whole_cluster_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _commands
    from cli.main import _build_parser

    def fail_if_called(**_kwargs: object) -> int:
        raise AssertionError("cmd_update must not run: the refusal is the parse-layer gate")

    monkeypatch.setattr(_commands, "cmd_update", fail_if_called)
    args = _build_parser().parse_args(["cluster", "update", "--target", "macmini", "--force"])
    assert args.func(args) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_parse_gate_refuses_target_sha_without_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _commands
    from cli.main import _build_parser

    def fail_if_called(**_kwargs: object) -> int:
        raise AssertionError("cmd_update must not run: the refusal is the parse-layer gate")

    monkeypatch.setattr(_commands, "cmd_update", fail_if_called)
    full_sha = "abc123" + "0" * 34
    args = _build_parser().parse_args(["cluster", "update", "--target-sha", full_sha])
    assert args.func(args) == 2
    assert "--target-sha requires --target" in capsys.readouterr().err


def test_parse_gate_passes_a_valid_target_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import commands as _commands
    from cli.main import _build_parser

    seen: dict[str, object] = {}

    def fake_cmd_update(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(_commands, "cmd_update", fake_cmd_update)
    args = _build_parser().parse_args(["cluster", "update", "--target", "macmini"])
    assert args.func(args) == 0
    assert seen["target"] == "macmini"
    assert seen["target_sha"] is None
