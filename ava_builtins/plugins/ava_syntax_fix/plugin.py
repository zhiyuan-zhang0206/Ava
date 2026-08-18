"""Built-in syntax-fix hook: auto-fix common Python syntax errors before exec.

Pipeline:
1. Chinese punctuation -> ASCII   (hand-written, ruff does not cover this)
2. Missing imports -> auto-add    (ruff F821 finds undefined names; curated
                                   tables map them to imports: stdlib modules,
                                   common from-import names like Path, and
                                   installed third-party aliases like np)
3. Invalid escape sequences fixed (hand-written via tokenize; ruff/3.12 only warn)
4. ruff check --fix               (unused imports, formatting)
5. ruff format                    (style normalization; gated)
6. Deterministic fixes            (11 patterns from production log analysis,
                                   each individually compile-guarded: unicode
                                   punct, f-string expressions, indentation,
                                   unterminated string->triple-quote, nested
                                   triple-quote escaping, nested inner-quote
                                   escaping, string newlines->triple-quote,
                                   unclosed triple-quote, missing comma,
                                   backslash trailing, bracket matching)
7. compile()                      (syntax + semantic check)
8. LLM repair                     (only if compile still fails -- a strong
                                   reasoning model rewrites the source)

If all steps pass: same-ID AIMessage replacement, agent unaware.
If compile() fails and LLM repair also fails: inject ToolMessage with the
error + skip subprocess. The agent gets immediate feedback without wasting a
subprocess round-trip.

Steps 1-3, 4-5, and 8 live in sibling modules (_punct / _imports / _escapes /
_ruff / _llm_repair); this file is the pipeline + hook. Split 2026-08-07
(Task #1011) to bring ava_builtins under the 800-line hard ceiling.
"""

from __future__ import annotations

__description__ = "Auto-fix common Python syntax errors before subprocess exec"

from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from loguru import logger

from agent.graph._context import AvaContext, agent_id_from_config
from agent.graph._tool_calls import (
    first_tool_call_code,
    merge_multiple_execute_code_tool_calls,
    replace_single_execute_code,
)
from agent.hooks import Hook, register_before_exec
from agent.messages import exec_output_message
from agent.state import AgentState
from shared.config import settings

from ._deterministic_fixes import apply_all_deterministic_fixes
from ._escapes import _fix_invalid_escapes
from ._imports import (
    _detect_missing_imports,
    _insert_imports,
)
from ._imports import (
    _is_stdlib_module as _is_stdlib_module,  # re-exported for tests
)
from ._imports import (
    _ruff_executable as _ruff_executable,
)
from ._llm_repair import (
    _extract_text as _extract_text,  # re-exported for tests
)
from ._llm_repair import (
    _llm_repair_syntax,
    _render_syntax_error,
)
from ._llm_repair import (
    _strip_code_fence as _strip_code_fence,
)
from ._punct import _fix_chinese_punctuation
from ._ruff import _ruff_fix, _ruff_format

# ---------------------------------------------------------------------------
# Main hook
# ---------------------------------------------------------------------------


def _emit_syntax_fix_event(
    *,
    fix_type: str,
    before: str,
    after: str | None,
    fixes: list[str],
    note: str,
) -> None:
    """Record one syntax_fix event for the trace-v2 content-retention contract.

    These events are the zero-loss record of every code mutation this hook
    performs, long after the trace mirror retention or checkpoint trim would
    have dropped the context around them:

    - fix_type="rough" — deterministic rules only (chinese_punct, missing
      imports, ruff, the 11 compile-guarded patterns). Stores `before` only:
      the after state is exactly `_apply_fix_pipeline(before)`, so it is
      replayable and storing it would double the payload for nothing.
    - fix_type="lm" — an LLM rewrote the source (compile() still failed after
      all deterministic steps). The rewrite is NOT replayable, so `after` is
      stored alongside `before` (which is the deterministic-fixed source the
      LLM actually saw).

    Payload shape (loguru extra -> agent_events.payload jsonb):
    {fix_type, fixes, before, after?, msg}. Volume is linear in the code
    block size (KB-scale), which is the design point: long retention, no
    information loss.
    """
    attrs: dict[str, Any] = {
        "fix_type": fix_type,
        "fixes": fixes,
        "before": before,
    }
    if after is not None:
        attrs["after"] = after
    logger.info(
        "[syntax_fix] {note}",
        event="syntax_fix",
        note=note,
        **attrs,
    )


def _apply_fix_pipeline(code: str) -> tuple[str, list[str]]:
    """Run the deterministic fix pipeline (steps 1-6): Chinese punctuation,
    missing imports, invalid escapes, ruff --fix, ruff format (gated), and the
    11 compile-guarded deterministic patterns. Returns (fixed_code,
    fixes_applied) -- fixes_applied is non-empty iff some step changed the code.
    """
    fixes_applied: list[str] = []
    fixed_code = code

    # Step 1: Chinese punctuation
    fixed_code, n = _fix_chinese_punctuation(fixed_code)
    if n > 0:
        fixes_applied.append(f"chinese_punct({n})")

    # Step 2: Missing imports
    missing = _detect_missing_imports(fixed_code)
    if missing:
        fixed_code = _insert_imports(fixed_code, missing)
        fixes_applied.append(f"missing_imports({len(missing)})")

    # Step 3: Invalid-escape-sequence fix (before ruff so ruff sees clean source)
    fixed_code, n_esc = _fix_invalid_escapes(fixed_code)
    if n_esc > 0:
        fixes_applied.append(f"invalid_escape({n_esc})")

    # Step 4: ruff --fix (unused imports, formatting)
    ruff_before = fixed_code
    fixed_code = _ruff_fix(fixed_code)
    if fixed_code != ruff_before:
        fixes_applied.append("ruff")

    # Step 5: ruff format (style normalization) — pulls in-context code toward
    # canonical formatting. Off the deterministic-correctness path: it never
    # fixes an error, only restyles, so it is gated and trivially skippable.
    fixed_code, fmt_applied = _maybe_ruff_format(fixed_code)
    if fmt_applied:
        fixes_applied.append("ruff_format")

    # Step 6: Deterministic fixes — 11 known failure patterns from production
    # log analysis (string-delimiter repair, nested triple-quote / inner-quote
    # escaping, indentation, bracket matching, f-string expressions, etc.) fixed
    # here before compile(). Each fix is individually compile-guarded and the
    # first one that makes the source compile wins, so the LLM repair step rarely
    # needs to run.
    det_fixed, det_applied = apply_all_deterministic_fixes(fixed_code)
    if det_applied:
        logger.info(
            "[{label}] det_fix: patterns={patterns}",
            label="syntax_fix",
            patterns=",".join(det_applied),
        )
        fixed_code = det_fixed
        fixes_applied.extend(det_applied)

    return fixed_code, fixes_applied


def _maybe_ruff_format(code: str) -> tuple[str, bool]:
    """Step 5 of the pipeline: run `ruff format` (style-only normalization)
    when gated on; returns (code, applied)."""
    if not settings.sandbox.syntax_fix_ruff_format:
        return code, False
    formatted = _ruff_format(code)
    return formatted, formatted != code


def _finalize_success(
    fixed_msg: AIMessage,
    fixes_applied: list[str],
    merged_msg: AIMessage | None,
    before_code: str | None = None,
) -> dict[str, object] | None:
    """Emit the fixed message, or None to pass the original through untouched
    when nothing changed and no merge occurred."""
    if not fixes_applied and merged_msg is None:
        return None

    if fixes_applied:
        logger.info(
            "[{label}] applied: {fixes}",
            label="syntax_fix",
            fixes=",".join(fixes_applied),
        )
        # Trace-v2 retention event: deterministic fix, before-only (after is
        # replayable via _apply_fix_pipeline). `before_code` is the original
        # agent-written source — the pre-fix truth.
        _emit_syntax_fix_event(
            fix_type="rough",
            before=before_code or "",
            after=None,
            fixes=fixes_applied,
            note=f"deterministic fixes applied: {','.join(fixes_applied)}",
        )
    # Code is valid and was fixed -- silent same-ID replacement
    return {"messages": [fixed_msg]}


async def _handle_compile_failure(
    e: SyntaxError,
    fixed_code: str,
    fixes_applied: list[str],
    last_msg: AIMessage,
    fixed_msg: AIMessage,
    tool_call_id: str,
) -> dict[str, object]:
    """Last-resort handling for source that still does not compile: hand it to
    the LLM repair model, or surface the error to the agent (skipping the
    subprocess). Returns the graph dict to emit.
    """
    rendered_error = _render_syntax_error(e, fixed_code)

    # Last resort: hand the still-broken source + its error to a strong
    # reasoning model. Covers open-ended string-delimiter mistakes the
    # deterministic steps can't (multi-line value in single quotes, embedded
    # triple-quote, trailing quote against the closing delimiter).
    repaired = await _llm_repair_syntax(fixed_code, rendered_error)
    if repaired is not None and repaired != fixed_code:
        # _llm_repair_syntax only returns source that compiles.
        logger.info(
            "[{label}] llm_repair: {msg} (deterministic fixes: {fixes})",
            label="syntax_fix",
            msg=str(e),
            fixes=",".join(fixes_applied) or "none",
        )
        # Trace-v2 retention event: an LLM rewrote the source — not replayable,
        # so before AND after are stored. `before` is the deterministic-fixed
        # source the LLM actually saw (identical to the original when no
        # deterministic fix fired); `after` is the repair output.
        _emit_syntax_fix_event(
            fix_type="lm",
            before=fixed_code,
            after=repaired,
            fixes=fixes_applied,
            note=f"llm_repair: {e}",
        )
        return {"messages": [replace_single_execute_code(last_msg, repaired)]}

    logger.info(
        "[{label}] compile_failed: {msg} (fixes: {fixes})",
        label="syntax_fix",
        msg=str(e),
        fixes=",".join(fixes_applied) or "none",
    )
    # Unfixable -- surface the error to the agent so it retries, skipping the
    # subprocess. Format mimics Python's native traceback.
    error_text = "Code execution output:\n\n" + rendered_error + "\n"

    tool_msg = exec_output_message(
        content=error_text,
        tool_call_id=tool_call_id,
        exit_code=1,
        cancelled=False,
    )
    return {
        "goto": "after_exec",
        "messages": [fixed_msg, tool_msg],
        "halted": False,  # route to before_llm -> agent retries
    }


class _SyntaxFixHook(Hook):
    """Auto-fix common Python syntax errors before subprocess execution.

    Pipeline: Chinese punctuation -> missing imports -> ruff --fix -> compile().

    - If code is valid after fixes: same-ID AIMessage replacement, silent.
    - If code is still broken: inject a ToolMessage with the compile() error
      and skip the subprocess (goto after_exec). Agent retries immediately.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        last_msg = state.messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        merged_msg = merge_multiple_execute_code_tool_calls(
            last_msg,
            agent_id=agent_id_from_config(config),
            location="before_exec",
        )
        if merged_msg is not None:
            last_msg = merged_msg

        tool_calls = last_msg.tool_calls
        code = first_tool_call_code(tool_calls)
        if not code:
            if merged_msg is not None:
                return {"messages": [merged_msg]}
            return None

        fixed_code, fixes_applied = _apply_fix_pipeline(code)

        # Build fixed AIMessage (same ID = in-place replacement by add_messages).
        # The tool_use block inside content is updated in sync too; otherwise
        # LangChain may still carry the old code / extra tool_use blocks when
        # converting to an Anthropic payload.
        fixed_msg = replace_single_execute_code(last_msg, fixed_code)

        # Step 7: compile() pre-check.
        # Catches syntax errors + semantic errors (duplicate args, break outside
        # loop, etc.) that ast.parse() would miss. Uses '<agent_code>' as
        # filename so error messages reference the right source.
        try:
            compile(fixed_code, "<agent_code>", "exec")
        except SyntaxError as e:
            return await _handle_compile_failure(
                e,
                fixed_code,
                fixes_applied,
                last_msg,
                fixed_msg,
                tool_calls[0]["id"] or "",
            )

        return _finalize_success(fixed_msg, fixes_applied, merged_msg, before_code=code)


syntax_fix_before_exec = _SyntaxFixHook()
register_before_exec(syntax_fix_before_exec)
