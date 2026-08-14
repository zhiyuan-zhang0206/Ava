"""`ava notices` — operator CLI over the notice queue (Task #949).

Monkeypatched shared.http_dial + gateway base; asserts arg plumbing, filters,
resolve actions and the clear flow.
"""

from __future__ import annotations

from typing import Any

import pytest


class _Resp:
    def __init__(self, status_code: int, json_body: Any = None) -> None:
        self.status_code = status_code
        self._json = json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


def _notice(nid: int, agent: int, *, title: str = "T", require_response: bool = False) -> dict:
    return {
        "id": nid,
        "agent_id": agent,
        "agent_label": "test",
        "title": title,
        "content": None,
        "priority": "P2",
        "require_response": require_response,
        "blocking": False,
    }


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple]]:
    """Stub shared.http_dial get/post + machine helpers; record calls."""
    calls: dict[str, list[tuple]] = {"get": [], "post": []}

    def fake_get(url: str, **kw: Any) -> _Resp:
        calls["get"].append((url, kw))  # pyright: ignore[reportUnknownMemberType]
        return _Resp(200, [])

    def fake_post(url: str, **kw: Any) -> _Resp:
        calls["post"].append((url, kw))  # pyright: ignore[reportUnknownMemberType]
        return _Resp(201)

    monkeypatch.setattr("shared.http_dial.get", fake_get)
    monkeypatch.setattr("shared.http_dial.post", fake_post)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", lambda: {"X": "1"})
    return calls


def test_list_passes_include_awaiting(dial: dict[str, list[tuple]]) -> None:
    from cli.commands.notices import cmd_notices_list

    assert cmd_notices_list(agent_id=None, priority=None, type_filter=None) == 0
    url, kw = dial["get"][0]
    assert url == "http://gw/api/notices/open"
    assert kw["params"] == {"include_awaiting": True, "limit": 200}


def test_list_filters_by_agent_and_type(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.notices import cmd_notices_list

    def fake_get(url: str, **kw: Any) -> _Resp:
        return _Resp(
            200,
            [
                _notice(1, 7, title="dec", require_response=True),
                _notice(2, 7, title="fyi"),
                _notice(3, 9, title="other agent"),
            ],
        )

    monkeypatch.setattr("shared.http_dial.get", fake_get)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    assert (
        cmd_notices_list(agent_id=7, priority=None, type_filter="fyi") == 0
    )  # prints filtered rows


def test_resolve_requires_reply_for_answer() -> None:
    from cli.commands.notices import cmd_notices_resolve

    assert cmd_notices_resolve(notice_id=1, agent_id=7, action="answer", reply=None) == 2


def test_resolve_posts_action(
    dial: dict[str, list[tuple]], capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.notices import cmd_notices_resolve

    assert cmd_notices_resolve(notice_id=5, agent_id=7, action="read", reply=None) == 0
    url, kw = dial["post"][0]
    assert url == "http://gw/api/agents/7/notices/5/resolve"
    assert kw["json"] == {"action": "read", "reply": None}
    assert "resolved" in capsys.readouterr().out


def test_clear_resolves_each_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.notices import cmd_notices_clear

    posted: list[dict] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        return _Resp(
            200,
            [
                _notice(1, 7, title="dec", require_response=True),
                _notice(2, 7, title="fyi"),
            ],
        )

    def fake_post(url: str, **kw: Any) -> _Resp:
        posted.append(kw["json"])  # pyright: ignore[reportUnknownMemberType]
        return _Resp(201)

    monkeypatch.setattr("shared.http_dial.get", fake_get)
    monkeypatch.setattr("shared.http_dial.post", fake_post)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    # force skips the prompt
    assert cmd_notices_clear(agent_id=7, force=True) == 0
    assert posted == [
        {"action": "dismiss", "reply": None},
        {"action": "read", "reply": None},
    ]


def test_list_stale_filters_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    """--stale keeps only notices whose agent is terminated (Task #1149)."""
    from cli.commands.notices import cmd_notices_list

    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        calls.append(url)
        if url == "http://gw/api/agents":
            return _Resp(
                200,
                [
                    {"agent_id": 7, "status": "terminated"},
                    {"agent_id": 9, "status": "running"},
                ],
            )
        return _Resp(
            200,
            [
                _notice(1, 7, title="dead agent"),
                _notice(2, 9, title="live agent"),
            ],
        )

    monkeypatch.setattr("shared.http_dial.get", fake_get)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    assert cmd_notices_list(agent_id=None, priority=None, type_filter=None, stale=True) == 0
    assert calls == ["http://gw/api/notices/open", "http://gw/api/agents"]


def test_clear_stale_resolves_only_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    """--stale clears terminated agents' notices, leaves live agents' alone."""
    from cli.commands.notices import cmd_notices_clear

    posted: list[tuple[str, dict[str, object]]] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        if url == "http://gw/api/agents":
            return _Resp(
                200,
                [
                    {"agent_id": 7, "status": "terminated"},
                    {"agent_id": 9, "status": "hibernating"},
                ],
            )
        return _Resp(
            200,
            [
                _notice(1, 7, title="dead agent", require_response=True),
                _notice(2, 9, title="hibernating agent"),
            ],
        )

    def fake_post(url: str, **kw: Any) -> _Resp:
        posted.append((url, kw["json"]))
        return _Resp(201)

    monkeypatch.setattr("shared.http_dial.get", fake_get)
    monkeypatch.setattr("shared.http_dial.post", fake_post)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    assert cmd_notices_clear(agent_id=None, force=True, stale=True) == 0
    assert len(posted) == 1
    url, body = posted[0]
    assert url == "http://gw/api/agents/7/notices/1/resolve"
    assert body == {"action": "dismiss", "reply": None}


def test_clear_stale_rejects_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.notices import cmd_notices_clear

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    assert cmd_notices_clear(agent_id=7, force=True, stale=True) == 2
