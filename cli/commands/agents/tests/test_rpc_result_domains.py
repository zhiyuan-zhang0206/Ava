"""Actual operator commands cannot display success for unknown RPC result vocabulary."""

import httpx
import pytest

from base.host.net import http_dial
from cli.commands.agents import control


def _response(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    response = httpx.Response(200, json=payload, request=httpx.Request("POST", "http://gateway"))

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(http_dial, "post", post)


@pytest.mark.parametrize("status", ["enqueued", "already_terminated"])
def test_cancel_keeps_supported_result_display(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    _response(monkeypatch, {"status": status})
    assert control.cmd_agents_cancel(7) == 0
    assert f"cancel: {status}" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [{"status": "later"}, {"status": None}, {}])
def test_cancel_unknown_or_missing_result_never_prints_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    _response(monkeypatch, payload)
    with pytest.raises((ValueError, KeyError)):
        control.cmd_agents_cancel(7)
    assert capsys.readouterr().out == ""


def _billing(mode: object, outcome: object) -> dict[str, object]:
    return {
        "mode": mode,
        "outcome": outcome,
        "balance": {"ok": True, "detail": "available"},
        "agents": [],
        "halted_alive": [],
    }


@pytest.mark.parametrize(
    ("mode", "outcome", "exit_code"),
    [
        ("dry_run", "preview", 0),
        ("execute", "executed", 0),
        ("execute", "refused", 1),
    ],
)
def test_billing_run_keeps_display_and_exit_semantics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    outcome: str,
    exit_code: int,
) -> None:
    _response(monkeypatch, _billing(mode, outcome))
    assert control.cmd_agents_resurrect_billing(execute=mode == "execute") == exit_code
    assert f"mode: {mode} — outcome: {outcome}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [("later", "executed"), ("execute", "later"), (None, "preview"), ("dry_run", None)],
)
def test_billing_unknown_result_never_prints_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: object,
    outcome: object,
) -> None:
    _response(monkeypatch, _billing(mode, outcome))
    with pytest.raises(ValueError):
        control.cmd_agents_resurrect_billing(execute=True)
    assert capsys.readouterr().out == ""
