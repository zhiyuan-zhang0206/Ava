from __future__ import annotations

import json
from typing import Any, cast

import jsonschema
import pytest

from base.log.tests.log_records import loguru_records as loguru_records

from ..computer.input import CLIPBOARD_MAX_CHARS
from .test_computer_input import Desktop
from .test_computer_input import desktop as desktop

MARKER = "do-not-leak-this-text"


def _result(response: Any) -> dict[str, Any]:
    assert response["ok"] is True, response
    return json.loads(cast(dict[str, Any], response["result"])["content"][0]["text"])


def _requests(desktop: Desktop, method: str) -> list[dict[str, Any]]:
    return [req for req in desktop.requests if req["method"] == method]


def _action(desktop: Desktop) -> Any:
    return next(event for event in desktop.events if event.event_name == "computer_action")


async def test_write_sends_text_and_reports_only_a_length(desktop: Desktop) -> None:
    response = await desktop.call("clipboard_write", {"text": MARKER, "task_id": 42})
    assert _result(response) == {"written": len(MARKER)}
    assert _requests(desktop, "clipboard_write") == [
        {"id": 1, "method": "clipboard_write", "text": MARKER}
    ]
    assert desktop.clipboard == MARKER
    assert desktop.daemon._screen.holder == 7
    assert any(event.event_name == "computer_session_start" for event in desktop.events)


async def test_read_returns_text_types_and_length(desktop: Desktop) -> None:
    desktop.clipboard = "héllo \u4e16\u754c"
    result = _result(await desktop.call("clipboard_read", {}))
    assert result == {
        "text": "héllo \u4e16\u754c",
        "types": ["public.utf8-plain-text"],
        "length": 8,
        "truncated": False,
    }
    assert _requests(desktop, "clipboard_read") == [{"id": 1, "method": "clipboard_read"}]


async def test_read_without_text_is_explicitly_null_not_empty(desktop: Desktop) -> None:
    desktop.clipboard = None
    result = _result(await desktop.call("clipboard_read", {}))
    assert result["text"] is None and result["length"] == 0 and result["types"] == []
    assert _action(desktop).attributes["coords"] == "no text"


async def test_audit_records_a_length_never_the_text(
    desktop: Desktop, loguru_records: list[dict[str, Any]]
) -> None:
    await desktop.call("clipboard_write", {"text": MARKER, "task_id": 42})
    await desktop.call("clipboard_read", {"task_id": 42})
    actions = [event for event in desktop.events if event.event_name == "computer_action"]
    assert [a.attributes["action"] for a in actions] == ["clipboard_write", "clipboard_read"]
    assert [a.attributes["coords"] for a in actions] == [f"{len(MARKER)} chars"] * 2
    assert all(a.attributes["outcome"] == "ok" for a in actions)
    assert MARKER not in repr([event.attributes for event in desktop.events])
    assert MARKER not in repr(loguru_records)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"text": 123},
        {"text": None},
        {"text": ["x"]},
        {"text": "x", "password": MARKER},
        {"text": MARKER + "a" * CLIPBOARD_MAX_CHARS},
    ],
)
async def test_write_rejects_bad_input_before_the_helper_and_never_echoes_it(
    desktop: Desktop, args: dict[str, Any], loguru_records: list[dict[str, Any]]
) -> None:
    response = await desktop.call("clipboard_write", args)
    assert response["ok"] is False
    assert MARKER not in response["error"]
    assert not any(req["method"].startswith("clipboard") for req in desktop.requests)
    assert desktop.clipboard is None
    assert MARKER not in repr([event.attributes for event in desktop.events])
    assert MARKER not in repr(loguru_records)


async def test_write_accepts_exactly_the_longest_text(desktop: Desktop) -> None:
    text = "a" * CLIPBOARD_MAX_CHARS
    assert _result(await desktop.call("clipboard_write", {"text": text})) == {
        "written": CLIPBOARD_MAX_CHARS
    }


async def test_read_takes_no_arguments(desktop: Desktop) -> None:
    response = await desktop.call("clipboard_read", {"text": "x"})
    assert response["ok"] is False and "no arguments" in response["error"]
    assert not any(req["method"] == "clipboard_read" for req in desktop.requests)


@pytest.mark.parametrize(
    "tool,args", [("clipboard_read", {}), ("clipboard_write", {"text": MARKER})]
)
async def test_old_helper_fails_explicitly_without_touching_the_clipboard(
    desktop: Desktop, tool: str, args: dict[str, Any]
) -> None:
    desktop.clipboard_capable = False
    response = await desktop.call(tool, args)
    assert response["ok"] is False and "clipboard_v1" in response["error"]
    assert MARKER not in response["error"]
    assert not any(req["method"].startswith("clipboard") for req in desktop.requests)
    assert desktop.clipboard is None
    assert _action(desktop).attributes["outcome"] == "error"


async def test_helper_failure_is_audited_without_the_text(desktop: Desktop) -> None:
    desktop.denied = "the clipboard did not accept the text"
    response = await desktop.call("clipboard_write", {"text": MARKER})
    assert response["ok"] is False and "did not accept" in response["error"]
    assert MARKER not in repr([event.attributes for event in desktop.events])
    assert _action(desktop).attributes["outcome"] == "error"


async def test_clipboard_calls_do_not_move_the_tracked_pointer(desktop: Desktop) -> None:
    desktop.daemon._pointer = (500, 600)
    await desktop.call("clipboard_write", {"text": "x"})
    await desktop.call("clipboard_read", {})
    assert desktop.daemon._pointer == (500, 600)


async def test_schemas_are_strict_and_describe_the_privacy_contract(desktop: Desktop) -> None:
    listed = await desktop.daemon._dispatch({"id": 1, "method": "list_tools"})
    assert listed["ok"] is True
    tools = {tool["name"]: tool for tool in cast(list[dict[str, Any]], listed["result"])}
    write = tools["clipboard_write"]["input_schema"]
    jsonschema.validate({"text": "x", "task_id": 1}, write)
    assert write["required"] == ["text"]
    for bad in (
        {},
        {"text": 1},
        {"text": "x", "extra": 1},
        {"text": "a" * (CLIPBOARD_MAX_CHARS + 1)},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, write)
    read = tools["clipboard_read"]["input_schema"]
    jsonschema.validate({}, read)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"text": "x"}, read)
    assert "audit" in tools["clipboard_write"]["description"]
    assert "overwrites" in tools["clipboard_write"]["description"]
    assert "password" in tools["clipboard_read"]["description"]
