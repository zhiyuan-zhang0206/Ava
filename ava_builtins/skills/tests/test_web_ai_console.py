"""Unit tests for the web-ai console child (ava_builtins/skills/integrations/web-ai/console/scripts/ask.py).

The browser-driving half lives in webchat (covered by test_web_ai_webchat.py);
this locks the console-only mapping from a webchat `ask_many` row to the result
row the agent reads — specifically the error-row pass-through and the `ok` rule
(a timed-out partial is not `ok`). Loading ask.py also execs webchat via its own
__file__-derived sys.path guard; no browser is driven.
"""

from __future__ import annotations

from tests.skills import load_skill_script

ask = load_skill_script(
    "integrations", "web-ai", "console", "scripts", "ask.py", name="web_ai_console_ask_under_test"
)


def test_to_row_error_row_passes_through_as_not_ok() -> None:
    row = ask._to_row(
        {"site": "claude", "label": "Claude", "error": "RuntimeError: composer not found"}
    )
    assert row == {
        "site": "claude",
        "label": "Claude",
        "ok": False,
        "error": "RuntimeError: composer not found",
    }


def test_to_row_completed_answer_is_ok() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is True
    assert row["chars"] == len("the answer")
    assert row["answer"] == "the answer"


def test_to_row_incomplete_partial_is_not_ok() -> None:
    # A timed-out run can hand back a transient placeholder as the partial; `ok`
    # requires completion, not just text.
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "Thinking",
            "complete": False,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is False
    assert row["complete"] is False


def test_to_row_empty_complete_answer_is_not_ok() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "",
            "complete": True,
            "url": "https://chatgpt.com/c/abc",
            "tab_kept": False,
        }
    )
    assert row["ok"] is False


def test_to_row_includes_chat_id_when_present() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc-123",
            "chat_id": "abc-123",
            "tab_kept": False,
        }
    )
    assert row["chat_id"] == "abc-123"


def test_to_row_chat_id_none_when_absent() -> None:
    row = ask._to_row(
        {
            "site": "chatgpt",
            "label": "ChatGPT",
            "submitted_via": "button",
            "answer": "the answer",
            "complete": True,
            "url": "https://chatgpt.com/c/abc-123",
            "tab_kept": False,
        }
    )
    assert row["chat_id"] is None
