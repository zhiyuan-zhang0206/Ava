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

# This module is the plugin's SDK **surface** — deliberately empty of
# registrations: everything this plugin does is agent-runtime behavior
# (a before_exec hook), so children do not need any of it. The registrations live
# in `agent_runtime.py`, imported only on the full path (see
# `agent/_extensions.py`; task #3633).
