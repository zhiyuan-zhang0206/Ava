"""The recent-conversation query both recall passes key on (`agent/graph/recall/conversation_query.py`)."""

from langchain_core.messages import AIMessage, HumanMessage

from agent.graph.recall.conversation_query import (
    QUERY_CHAR_CAP,
    QUERY_MESSAGES,
    build_conversation_query,
)
from agent.messages import NoteTag, inbound_message, system_note_message


def _inbound(text: str, n: int = 1) -> HumanMessage:
    return inbound_message(content=text, source="user", inbound_id=n, body_start=0)


def test_the_query_is_the_genuine_messages_oldest_first() -> None:
    messages = [
        _inbound("first question"),
        AIMessage(content="an answer", id="a1"),
        system_note_message(content="a framework note", tag=NoteTag.MEMORY),
        _inbound("follow-up", 2),
    ]

    assert build_conversation_query(messages) == "first question\nan answer\nfollow-up"


def test_only_the_last_few_messages_are_read() -> None:
    messages = [_inbound(f"message {n}", n) for n in range(QUERY_MESSAGES + 4)]

    lines = build_conversation_query(messages).splitlines()

    assert len(lines) == QUERY_MESSAGES
    assert lines[-1] == f"message {QUERY_MESSAGES + 3}"


def test_a_huge_paste_is_cut_to_its_tail() -> None:
    query = build_conversation_query([_inbound("x" * (QUERY_CHAR_CAP * 3) + "END")])

    assert len(query) == QUERY_CHAR_CAP
    assert query.endswith("END")


def test_multimodal_content_contributes_its_text_blocks() -> None:
    message = AIMessage(
        content=[{"type": "text", "text": "see this"}, {"type": "image", "data": "..."}], id="a1"
    )

    assert build_conversation_query([message]) == "see this"


def test_a_conversation_of_notes_alone_is_an_empty_query() -> None:
    assert build_conversation_query([system_note_message(content="n", tag=NoteTag.MEMORY)]) == ""
