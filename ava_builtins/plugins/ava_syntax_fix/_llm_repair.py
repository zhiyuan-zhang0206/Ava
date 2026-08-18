"""
Step 8 of the syntax-fix pipeline: LLM repair.

A fast reasoning model rewrites source that still fails compile()
after all deterministic steps. Split out of plugin.py
(2026-08-07, Task #1011).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from loguru import logger

# ---------------------------------------------------------------------------
# 5. LLM repair (only on the rare path where deterministic fixes left it broken)
# ---------------------------------------------------------------------------

_REPAIR_MODEL = "deepseek-v4-flash"
_REPAIR_TIMEOUT = 60.0  # per-attempt; flash model responds much faster
_REPAIR_MAX_ATTEMPTS = 3  # single-shot is probabilistic; feed the error back and retry
_REPAIR_SYSTEM = (
    "You are a Python syntax repair tool. The user message contains Python "
    "source that failed to compile, followed by its SyntaxError. Return the "
    "corrected source code and nothing else -- no markdown fences, no prose, no "
    "commentary. Make the smallest change that resolves the error while "
    "preserving the code's intent and structure; do not add or remove unrelated "
    "lines. Frequent causes: a multi-line value written with single/double "
    "quotes that should be a triple-quoted string; a triple-quoted string whose "
    "body contains an unescaped triple-quote or ends with a quote adjacent to "
    "the closing delimiter; an f-string with an empty or malformed `{}` field."
)

_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n```\s*$", re.DOTALL)


def _render_syntax_error(e: SyntaxError, source: str) -> str:
    """Render a SyntaxError as Python's native traceback would -- a
    `File ... line N` header, the offending source line, a caret, and the
    `SyntaxError: msg` line. Reused for both the LLM repair prompt and the
    fallback message the agent receives.
    """
    lines: list[str] = []
    if e.lineno:
        lines.append(f'  File "<agent_code>", line {e.lineno}')
        # compile() does not set .text when the source is a string, so fall
        # back to slicing the offending line out of the source ourselves.
        source_line = e.text.rstrip() if e.text else ""
        if not source_line:
            code_lines = source.split("\n")
            if e.lineno <= len(code_lines):
                source_line = code_lines[e.lineno - 1]
        if source_line:
            lines.append(f"    {source_line}")
        if e.offset:
            lines.append(f"    {' ' * max(0, e.offset - 1)}^")
    lines.append(f"SyntaxError: {e.msg}")
    return "\n".join(lines)


def _extract_text(content: Any) -> str:
    """Flatten an LLM response content into plain text. A thinking model returns
    a list of blocks (thinking + text); keep only the text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _strip_code_fence(text: str) -> str:
    """Strip one surrounding markdown code fence if the model wrapped its output
    despite instructions not to."""
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


async def _repair_once(llm: Any, messages: list[Any]) -> str | None:
    """One LLM repair attempt: invoke with timeout, then flatten + strip the
    response. Returns the candidate source, or None when the model is
    unavailable (missing key, timeout, empty output)."""
    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=_REPAIR_TIMEOUT)
    except (
        Exception
    ) as exc:  # best-effort: any failure degrades to surfacing the error to the agent
        logger.warning(
            "[{label}] llm_repair unavailable: model={model} elapsed={elapsed:.1f}s err={err}",
            label="syntax_fix",
            model=_REPAIR_MODEL,
            elapsed=time.monotonic() - started,
            err=f"{type(exc).__name__}: {exc}",
        )
        return None
    return _strip_code_fence(_extract_text(resp.content)).strip() or None


async def _llm_repair_syntax(code: str, rendered_error: str) -> str | None:
    """Fast-model repair for a syntax error the deterministic pipeline could
    not fix, running deepseek-v4-flash (delimiter repair
    needs to infer the author's intent). Single-shot output is probabilistic, so
    each attempt's result is compiled and, if still broken, the new error is fed
    back for up to _REPAIR_MAX_ATTEMPTS rounds.

    Returns source that compiles, or None when every attempt fails or the model
    is unavailable (missing key, timeout, empty output) -- the caller then
    degrades to surfacing the error to the agent. A non-None return is
    guaranteed to compile.
    """
    from shared.lm.factory import build_chat_model

    try:
        llm = build_chat_model(_REPAIR_MODEL)
    except (
        Exception
    ) as exc:  # best-effort: any failure degrades to surfacing the error to the agent
        logger.warning(
            "[{label}] llm_repair unavailable: {err}",
            label="syntax_fix",
            err=f"{type(exc).__name__}: {exc}",
        )
        return None

    messages: list[Any] = [
        SystemMessage(content=_REPAIR_SYSTEM),
        HumanMessage(content=f"{code}\n\n# --- compile error ---\n{rendered_error}"),
    ]
    for attempt in range(1, _REPAIR_MAX_ATTEMPTS + 1):
        logger.debug(
            "[{label}] llm_repair attempt {attempt}/{max_attempts}: model={model} code_lines={lines}",
            label="syntax_fix",
            attempt=attempt,
            max_attempts=_REPAIR_MAX_ATTEMPTS,
            model=_REPAIR_MODEL,
            lines=len(code.splitlines()),
        )
        candidate = await _repair_once(llm, messages)
        if candidate is None:
            return None
        try:
            compile(candidate, "<agent_code>", "exec")
        except SyntaxError as retry_err:
            # Feed the still-broken attempt + its error back for another round.
            messages.append(AIMessage(content=candidate))
            messages.append(
                HumanMessage(
                    content=(
                        "That still fails to compile:\n"
                        f"{_render_syntax_error(retry_err, candidate)}\n"
                        "Return the full corrected source again, code only."
                    )
                )
            )
            continue
        return candidate
    return None
