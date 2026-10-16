from __future__ import annotations

import pytest

from base.agents.history.timeline import TimelineItem
from base.lm.catalog.decide.types import estimate_tokens
from gateway.agents.reply_suggestion.excerpt import build_excerpt, is_human_source


def item(
    kind: str,
    payload: str,
    index: int,
    source: str | None = None,
    source_message_id: str | None = None,
) -> TimelineItem:
    return TimelineItem.model_validate(
        {
            "item_id": f"{index}.0",
            "kind": kind,
            "payload": payload,
            "source": source,
            "created_at": "2026-10-11T00:00:00+00:00",
            "source_message_id": source_message_id,
        }
    )


@pytest.mark.parametrize(
    ("source", "human"),
    [
        ("user", True),
        ("ui:page:3", True),
        ("agent:7", False),
        ("system", False),
        ("watcher:1", False),
        ("schedule:2", False),
        (None, False),
    ],
)
def test_human_sources_match_the_timeline_model(source: str | None, human: bool) -> None:
    assert is_human_source(source) is human


def test_keeps_the_latest_user_message_and_the_last_turns_agent_text() -> None:
    items = [
        item("inbound_chat", "old question", 1, "user"),
        item("agent_chat", "old answer", 2),
        item("inbound_chat", "please deploy", 3, "user"),
        item("agent_reasoning", "thinking about it", 4),
        item("agent_chat", "Checking the build.", 5, source_message_id="ai-a"),
        item("agent_code", "run('x')", 6),
        item("code_output", "ok", 7),
        item("agent_chat", "Ready. Deploy now?", 8, source_message_id="ai-b"),
    ]

    excerpt = build_excerpt(items, 1500)

    assert excerpt is not None
    assert excerpt.text == (
        "[User]\nplease deploy\n\n[Assistant]\nChecking the build.\n\nReady. Deploy now?"
    )
    assert excerpt.last_message_id == "ai-b"


def test_a_wake_up_inbound_starts_a_new_turn_but_the_user_message_stays() -> None:
    items = [
        item("inbound_chat", "watch the queue", 1, "user"),
        item("agent_chat", "Watching.", 2),
        item("inbound_chat", "queue is empty", 3, "watcher:4"),
        item("agent_chat", "Nothing to do.", 4, source_message_id="ai-w"),
    ]

    excerpt = build_excerpt(items, 1500)

    assert excerpt is not None
    assert excerpt.text == "[User]\nwatch the queue\n\n[Assistant]\nNothing to do."


def test_no_agent_text_in_the_last_turn_means_no_excerpt() -> None:
    items = [
        item("inbound_chat", "hello", 1, "user"),
        item("agent_chat", "hi", 2),
        item("inbound_chat", "now do it", 3, "user"),
        item("agent_code", "run()", 4),
    ]

    assert build_excerpt(items, 1500) is None


def test_without_a_human_message_only_the_agent_text_remains() -> None:
    items = [item("inbound_chat", "tick", 1, "system"), item("agent_chat", "Need input?", 2)]

    excerpt = build_excerpt(items, 1500)

    assert excerpt is not None
    assert excerpt.text == "[Assistant]\nNeed input?"


def test_identity_falls_back_to_item_id_and_time_without_a_message_id() -> None:
    excerpt = build_excerpt([item("agent_chat", "Hello?", 4)], 1500)

    assert excerpt is not None
    assert excerpt.last_message_id == "4.0@2026-10-11T00:00:00+00:00"


@pytest.mark.parametrize("budget", [120, 300])
def test_the_excerpt_never_exceeds_the_budget_and_the_reply_keeps_its_newest_words(
    budget: int,
) -> None:
    long_reply = " ".join(f"word{n}" for n in range(400)) + " THE-END"
    items = [
        item("inbound_chat", "please deploy the release", 1, "user"),
        item("agent_chat", long_reply, 2),
    ]

    excerpt = build_excerpt(items, budget)

    assert excerpt is not None
    assert estimate_tokens(excerpt.text) <= budget
    assert "please deploy the release" in excerpt.text
    assert excerpt.text.endswith("THE-END")
    assert "…" in excerpt.text


def test_an_oversized_reply_still_keeps_the_user_message() -> None:
    items = [
        item("inbound_chat", "latest-question", 1, "user"),
        item("agent_chat", "x" * 4000, 2),
    ]

    excerpt = build_excerpt(items, 200)

    assert excerpt is not None
    assert "latest-question" in excerpt.text
    assert estimate_tokens(excerpt.text) <= 200


def test_an_oversized_user_message_keeps_its_newest_words_and_displaces_the_reply() -> None:
    items = [
        item("inbound_chat", "q " * 400 + "the-ask", 1, "user"),
        item("agent_chat", "ok", 2),
    ]

    excerpt = build_excerpt(items, 100)

    assert excerpt is not None
    assert excerpt.text.startswith("[User]")
    assert excerpt.text.endswith("the-ask")
    assert "[Assistant]" not in excerpt.text
    assert estimate_tokens(excerpt.text) <= 100


def test_non_ascii_text_is_counted_per_character() -> None:
    items = [item("agent_chat", "\u597d" * 500 + "\u7ed3\u5c3e", 2)]

    excerpt = build_excerpt(items, 100)

    assert excerpt is not None
    assert estimate_tokens(excerpt.text) <= 100
    assert excerpt.text.endswith("\u7ed3\u5c3e")


def test_a_budget_too_small_for_any_text_yields_nothing() -> None:
    assert build_excerpt([item("agent_chat", "hello", 2)], 3) is None


def test_a_non_positive_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        build_excerpt([item("agent_chat", "hello", 2)], 0)
