"""Browser network diagnostics keep temporary pages out of the user's tab strip."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.supervision.healthchecks import browser_reach as hc


def _wire_cdp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch_outcome: str = "ok",
    hidden_supported: bool = True,
) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    browser = AsyncMock()
    browser.__aenter__.return_value = browser
    page = AsyncMock()
    page.__aenter__.return_value = page
    browser.recv.side_effect = [
        json.dumps({"id": 1, "result": {"targetId": "canary"}}),
        json.dumps({"id": 3, "result": {"success": True}}),
    ]
    if not hidden_supported:
        browser.recv.side_effect = [
            json.dumps({"id": 1, "error": {"message": "Hidden targets are unsupported"}}),
        ]
    if fetch_outcome == "timeout":
        page.recv.side_effect = TimeoutError
    else:
        page.recv.return_value = json.dumps(
            {"id": 2, "result": {"result": {"value": fetch_outcome}}}
        )
    connect = MagicMock(side_effect=[browser, page])
    monkeypatch.setattr(hc.websockets, "connect", connect)

    def version(_url: str, _timeout: float) -> dict[str, object]:
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test"}

    monkeypatch.setattr(hc, "_read_json", version)
    return browser, page, connect


@pytest.mark.parametrize(
    "fetch_outcome,expected",
    [("ok", "ok"), ("error:TypeError", "error"), ("timeout", "timeout")],
)
def test_canary_uses_hidden_shared_context_and_closes_after_fetch(
    monkeypatch: pytest.MonkeyPatch, fetch_outcome: str, expected: str
) -> None:
    browser, page, _connect = _wire_cdp(monkeypatch, fetch_outcome=fetch_outcome)

    assert hc.canary(9222, "https://example.invalid/health", 1).outcome == expected

    create, close = [json.loads(call.args[0]) for call in browser.send.await_args_list]
    assert create["method"] == "Target.createTarget"
    assert create["params"]["hidden"] is True
    assert create["params"]["background"] is True
    assert "browserContextId" not in create["params"]
    assert close["method"] == "Target.closeTarget"
    assert close["params"]["targetId"] == "canary"
    evaluate = json.loads(page.send.await_args_list[0].args[0])
    assert evaluate["method"] == "Runtime.evaluate"
    assert 'fetch("https://example.invalid/health"' in evaluate["params"]["expression"]


def test_canary_rejected_hidden_target_never_falls_back_to_visible_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, page, connect = _wire_cdp(monkeypatch, hidden_supported=False)

    result = hc.canary(9222, "https://example.invalid/health", 1)

    assert result.outcome == "skip"
    assert "Hidden targets are unsupported" in result.detail
    browser.send.assert_awaited_once()
    create = json.loads(browser.send.await_args_list[0].args[0])
    assert create["params"]["hidden"] is True
    assert connect.call_count == 1
    page.send.assert_not_awaited()


def test_canary_unexpected_failure_is_not_reachability_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_port: int, _url: str, _timeout_s: float) -> hc._CanaryResult:
        raise RuntimeError("CDP failed")

    monkeypatch.setattr(hc, "_canary_async", fail)
    assert hc.canary(9222, "https://example.invalid/", 1).outcome == "skip"
