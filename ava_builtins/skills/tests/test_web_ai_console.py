from __future__ import annotations

from ava_builtins.skills.tests.skill_script import load_skill_script

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
