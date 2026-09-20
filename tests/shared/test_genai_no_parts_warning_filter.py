"""langchain-google-genai's per-message 'no Gemini parts' warning is dropped (#3317).

Bug: `_parse_chat_history` logs one WARNING for every AI message whose content
converts to no Gemini parts, and it logs it again on every later conversion of a
history that contains such a message — so a long agent conversation turns one
benign substitution into a permanent flood. A single long-context agent wrote
2,072 of these records into the file sink and the event stream in one day
(2026-09-13).

Fix: `_install_stdlib_intercept` attaches `_GenaiEmptyPartsWarningFilter` to the
root intercept handler. It drops exactly the two warning variants and lets every
other `langchain_google_genai` record through — including the library's own
"cannot be represented as a Gemini part" warning — which is why gating the whole
logger to ERROR was rejected.

The conversion cases below run through the library's own `_parse_chat_history`:
pure message conversion, no credentials, no network.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_google_genai.chat_models import _parse_chat_history

from shared.log import (
    _GenaiEmptyPartsWarningFilter,
    _install_stdlib_intercept,
    _StdlibInterceptHandler,
)

_MODEL = "gemini-2.5-flash"
_GENAI_CHAT_MODELS_LOGGER = "langchain_google_genai.chat_models"
# Prefix shared by both dropped variants; the pass-through warning below does
# not contain it.
_EMPTY_PARTS_MARKER = "converted to no Gemini parts"


def _messages(loguru_records: list[dict]) -> list[str]:
    """Loguru messages captured by the `loguru_records` fixture."""
    return [
        str(record["message"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for record in loguru_records
    ]


def _convert(ai_message: AIMessage) -> None:
    """Run the library's real history conversion over one AI message."""
    _parse_chat_history([HumanMessage(content="hi"), ai_message], model=_MODEL)


def test_empty_ai_message_warning_does_not_reach_loguru(loguru_records: list[dict]) -> None:
    """`AIMessage(content=[])` converts to no parts -> the library substitutes
    an empty text part and warns; that warning must not reach loguru."""
    _install_stdlib_intercept()
    _convert(AIMessage(content=[]))
    assert not any(_EMPTY_PARTS_MARKER in message for message in _messages(loguru_records))


def test_dropped_content_blocks_warning_does_not_reach_loguru(loguru_records: list[dict]) -> None:
    """The "after all N content block(s) were dropped" variant is dropped too,
    while the library's distinct per-block warning still flows."""
    _install_stdlib_intercept()
    _convert(AIMessage(content=[{"type": "not_a_gemini_block", "payload": 1}]))
    messages = _messages(loguru_records)
    assert not any(_EMPTY_PARTS_MARKER in message for message in messages)
    assert any("cannot be represented as a Gemini part" in message for message in messages)


def test_other_records_from_the_same_logger_pass(loguru_records: list[dict]) -> None:
    """Only the two variants are dropped: other warnings and INFO records on the
    same logger still reach loguru — the reason a blanket setLevel(ERROR) was
    rejected."""
    _install_stdlib_intercept()
    genai_log = logging.getLogger(_GENAI_CHAT_MODELS_LOGGER)
    genai_log.warning("Dropping content block that cannot be represented as a Gemini part")
    genai_log.info("GenAI conversion completed")
    messages = _messages(loguru_records)
    assert "Dropping content block that cannot be represented as a Gemini part" in messages
    assert "GenAI conversion completed" in messages


def test_same_marker_from_another_logger_passes(loguru_records: list[dict]) -> None:
    """The drop is scoped to `langchain_google_genai` and its dotted children —
    an identical message from any other logger is untouched."""
    _install_stdlib_intercept()
    logging.getLogger("shared.llm_router").warning(
        "AI message at index 1 converted to no Gemini parts; using an empty text part."
    )
    assert any(_EMPTY_PARTS_MARKER in message for message in _messages(loguru_records))


def test_repeated_install_does_not_stack_filters(loguru_records: list[dict]) -> None:
    """`init_*` entry points may install the intercept more than once per process
    (#970): the second install must rebuild the handler instead of accumulating
    filters on top of the first."""
    _install_stdlib_intercept()
    _install_stdlib_intercept()
    genai_log = logging.getLogger(_GENAI_CHAT_MODELS_LOGGER)
    genai_log.warning(
        "AI message at index 7 converted to no Gemini parts; using an empty text part."
    )
    genai_log.warning("A real GenAI warning")
    messages = _messages(loguru_records)
    assert not any(_EMPTY_PARTS_MARKER in message for message in messages)
    assert messages.count("A real GenAI warning") == 1
    intercept_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, _StdlibInterceptHandler)
    ]
    assert len(intercept_handlers) == 1
    installed_filters = [
        flt
        for flt in intercept_handlers[0].filters
        if isinstance(flt, _GenaiEmptyPartsWarningFilter)
    ]
    assert len(installed_filters) == 1
