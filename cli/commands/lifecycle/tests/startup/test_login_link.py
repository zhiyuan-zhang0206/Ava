"""`meiva start`'s one-time browser login link.

The link is `<ui>/#ticket=<ticket>`: the ticket is minted by the gateway for the
home's human secret and travels in the fragment. A start that is not at a
terminal, has no UI, or holds no human secret mints nothing; a gateway failure
is reported without failing the start.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from base.config import settings
from base.host.net import http_dial
from cli.commands.lifecycle import login_link

_SECRET = "human-secret-for-link-tests"  # noqa: S105 — test fixture
_TICKET = "ticket.human-0123.abcDEF_-xyz"


class _Dial:
    """Stand-in for `base.host.net.http_dial.post` recording the call."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _reply(status: int = 200, body: dict[str, Any] | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://gateway.test/api/auth/login-ticket")
    return httpx.Response(
        status,
        json={"ticket": _TICKET, "expires_in_seconds": 120} if body is None else body,
        request=request,
    )


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _SECRET)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gateway.test:8000")
    monkeypatch.setattr(settings.gateway, "browser_origin", "")
    monkeypatch.setattr(settings.services, "frontend_healthcheck_url", "http://localhost:3000")
    monkeypatch.setattr(settings.services, "app_port", None)
    monkeypatch.setattr(login_link, "at_terminal", lambda: True)


def _dial(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception) -> _Dial:
    dial = _Dial(response)
    monkeypatch.setattr(http_dial, "post", dial)
    return dial


def test_ui_base_is_the_gate_entry_or_the_https_browser_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert login_link.ui_base({"gateway", "gate"}) == "http://localhost:3000"
    monkeypatch.setattr(settings.gateway, "browser_origin", "https://console.example/")
    assert login_link.ui_base({"gate"}) == "https://console.example"


def test_ui_base_without_gate_is_the_apps_reserved_port(monkeypatch: pytest.MonkeyPatch) -> None:
    assert login_link.ui_base({"gateway", "frontend"}) is None  # no reserved port to point at
    monkeypatch.setattr(settings.services, "app_port", 3101)
    assert login_link.ui_base({"gateway", "frontend"}) == "http://localhost:3101"
    assert login_link.ui_base({"gateway"}) is None


def test_prints_the_ticket_in_the_fragment_and_never_the_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dial = _dial(monkeypatch, _reply())
    login_link.print_login_link({"gateway", "gate"})
    out = capsys.readouterr().out

    assert f"http://localhost:3000/#ticket={_TICKET}" in out
    assert _SECRET not in out
    [(url, kwargs)] = dial.calls
    assert url == "http://gateway.test:8000/api/auth/login-ticket"
    assert kwargs["headers"] == {"Authorization": f"Bearer {_SECRET}"}


def test_skips_when_output_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(login_link, "at_terminal", lambda: False)
    dial = _dial(monkeypatch, _reply())
    login_link.print_login_link({"gateway", "gate"})
    out = capsys.readouterr().out

    assert dial.calls == []
    assert "#ticket=" not in out and "skipped" in out


def test_mints_nothing_without_a_ui_or_the_human_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dial = _dial(monkeypatch, _reply())
    login_link.print_login_link({"gateway"})
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    login_link.print_login_link({"gateway", "gate"})

    assert dial.calls == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize(
    "response",
    [
        httpx.ConnectError("refused"),
        _reply(403, {"detail": "requires the human cluster secret"}),
        _reply(200, {"ok": True}),
    ],
    ids=["unreachable", "refused", "no-ticket"],
)
def test_a_gateway_failure_is_reported_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: httpx.Response | Exception,
) -> None:
    _dial(monkeypatch, response)
    login_link.print_login_link({"gateway", "gate"})
    captured = capsys.readouterr()

    assert "could not mint a one-time UI login link" in captured.err
    assert "#ticket=" not in captured.out
