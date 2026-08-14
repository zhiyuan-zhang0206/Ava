"""Render exec tracebacks for two audiences with two different cuts.

Agent-written code runs under the pseudo-filename ``<agent_code>`` (the name
``compile()`` stamps in `_exec.py`). When that code raises, the raw traceback
threads through the exec harness above it and the SDK / plugin / standard-library
frames below it. Those frames are useless to the agent (it did not write them and
cannot fix them) and they leak framework internals into the model's context.

`format_agent_traceback` keeps only the ``<agent_code>`` frames plus the exception
type and message, recursively across chained causes/contexts. Non-agent frames
(harness, SDK, plugin, stdlib) are dropped silently — the agent's own frames
plus the exception type and message are always sufficient for debugging.
`format_full_traceback` is the unfiltered counterpart for the server logs, where a
genuine framework bug is diagnosed.

`register_agent_source` registers the agent's source in `linecache` so the
``<agent_code>`` frames (and a SyntaxError's caret) resolve their offending line —
exec'd code is otherwise invisible to `linecache`, which is why a raw exec traceback
shows ``File "<agent_code>", line N`` with no source line beneath it.
"""

from __future__ import annotations

import linecache
import re
import traceback
from traceback import StackSummary, TracebackException

AGENT_CODE_FILENAME = "<agent_code>"

# Regex matching PEP 657 fine-grained error location annotation lines — pure
# whitespace, ^, and ~ (the visual caret markers CPython 3.11+ inserts under
# the offending expression). These are visual aids for humans; an LLM identifies
# the expression from the source line itself, so they are stripped.
_CARET_LINE_RE = re.compile(r"^\s*[ ~^]+\s*$")


def _strip_caret_lines(text: str) -> str:
    return "".join(
        line for line in text.splitlines(keepends=True) if not _CARET_LINE_RE.match(line)
    )


_TB_HEADER = "Traceback (most recent call last):\n"

# Chain connectors, matching CPython's traceback wording. Kept as local constants
# rather than reaching into the private `traceback._cause_message` so the rendering
# does not depend on a stdlib internal that could move between versions.
_CAUSE_MESSAGE = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT_MESSAGE = "\nDuring handling of the above exception, another exception occurred:\n\n"

# SyntaxError and its subclasses carry their location (file/line/caret) in the
# exception object itself, formatted by `format_exception_only`, not in the stack.
# Matched by name so the check is stable across the 3.13 `exc_type` -> `exc_type_str`
# deprecation.
_SYNTAX_EXC_NAMES = frozenset({"SyntaxError", "IndentationError", "TabError"})


def register_agent_source(code: str) -> None:
    """Make ``<agent_code>`` frames resolve their source line in tracebacks.

    Stores the source under the ``<agent_code>`` key with a None mtime, which
    `linecache.checkcache` leaves untouched (no real file to validate against).
    Overwritten each turn with the current code; one agent per process means the
    shared key never races across agents.
    """
    linecache.cache[AGENT_CODE_FILENAME] = (
        len(code),
        None,
        code.splitlines(keepends=True),
        AGENT_CODE_FILENAME,
    )


def _exc_type_name(te: TracebackException) -> str:
    """Exception class name, across the 3.13 `exc_type` deprecation."""
    name = getattr(te, "exc_type_str", None)  # Python 3.13+
    if name is not None:
        return name
    return te.exc_type.__name__  # Python 3.12


def _filter_stack(stack: StackSummary) -> StackSummary:
    """Keep only ``<agent_code>`` frames.

    Harness frames above the first agent frame and library/framework frames below
    it are all dropped — only the agent's own code remains.
    """
    return StackSummary.from_list([f for f in stack if f.filename == AGENT_CODE_FILENAME])


def _format_level(te: TracebackException) -> str:
    """Format a single exception in the chain (no cause/context recursion)."""
    if _exc_type_name(te) in _SYNTAX_EXC_NAMES:
        # The stack would only hold harness frames; the agent-relevant location is
        # in the exception-only block, which carries the ``<agent_code>`` caret.
        return _strip_caret_lines("".join(te.format_exception_only()))

    kept = _filter_stack(te.stack)
    parts: list[str] = []
    if kept:
        parts.append(_TB_HEADER)
        parts.extend(kept.format())
    parts.extend(te.format_exception_only())
    return _strip_caret_lines("".join(parts))


def _format_chain(te: TracebackException) -> str:
    """Format `te` plus its cause/context chain, filtering every level."""
    parts: list[str] = []
    if te.__cause__ is not None:
        parts.append(_format_chain(te.__cause__))
        parts.append(_CAUSE_MESSAGE)
    elif te.__context__ is not None and not te.__suppress_context__:
        parts.append(_format_chain(te.__context__))
        parts.append(_CONTEXT_MESSAGE)
    parts.append(_format_level(te))
    return "".join(parts)


def format_agent_traceback(exc: BaseException) -> str:
    """Traceback for the agent: only the agent's own ``<agent_code>`` frames.

    Harness, SDK, plugin, and standard-library frames are dropped silently.
    PEP 657 caret annotation lines (``^^^^^``) are stripped — the source line
    itself is sufficient for the model. The exception type and message are always
    kept, and chained causes/contexts are filtered the same way. Always ends with
    a newline.
    """
    te = TracebackException.from_exception(exc)
    return _format_chain(te)


def format_full_traceback(exc: BaseException) -> str:
    """Unfiltered traceback for the server logs — every frame, for diagnosing a
    genuine framework or SDK bug the agent cannot fix."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


__all__ = [
    "AGENT_CODE_FILENAME",
    "format_agent_traceback",
    "format_full_traceback",
    "register_agent_source",
]
