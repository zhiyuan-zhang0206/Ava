"""CodeAct batching section — opt-in system-prompt guidance.

Owned as its own module (like `_capabilities.py`) because the section list in
`_system_prompt.py` is at its line ceiling; `_system_prompt` imports and
registers the section explicitly so the render order stays its reading order.
"""

from shared.config import settings


def _codeact_section() -> str:
    """Toggle via settings.agent.prompt_codeact_enabled (env
    AVA_SYSTEM_PROMPT_CODEACT, default off — opt-in). CodeAct batching: every
    `execute_code` call is one LLM API round-trip, so pack several operations
    into one call (batch file reads, fold branches into if-else logic) instead
    of many single-purpose calls. Off by default because it steers toward
    larger, denser tool calls — a per-cluster/per-agent choice, not a
    universal default."""
    if not settings.agent.prompt_codeact_enabled:
        return ""
    return (
        "# CodeAct \u2014 batch work into fewer calls\n\n"
        "Each `execute_code(code: str)` call is one LLM API round-trip, so pack "
        "several operations into a single call instead of many calls that each "
        "do one thing:\n\n"
        "- Read several files in one call (one read per file, same script) "
        "instead of one read per call.\n"
        "- When the next step could go several ways, compute both branches in "
        "the same script \u2014 plain if-else on values you already have \u2014 "
        "instead of running one branch, reading its output, then running the "
        "other.\n"
        "- Chain independent steps \u2014 fetch, transform, write \u2014 in one "
        "script rather than one call per step.\n\n"
        "You still receive every call's output back, so batching loses "
        "nothing but round-trips. Split into a second call only when the "
        "next step genuinely depends on the previous one's output."
    )
