"""The agent's single tool schema.

`execute_code` is the sole tool of the single-tool refactor — every
capability is reached through the Python namespace it runs, not through a
per-capability tool. See `future/single-tool-rearchitecture.md`.

This module holds only the tool *schema* (name + docstring + arg types that
`bind_tools` consumes); the actual run is dispatched by the exec node. The
chat-model factory that the schema is bound onto lives in `base/lm/factory.py`.

The package holds the helpers around the agent's own model calls, outside
the llm graph node (`agent/graph/llm/`) that drives them:

  - `cache.py` — invocation prep shared by the llm node and compaction:
    explicit Gemini cache binding with a plain-path fallback, and the
    stale-cache retry for single-shot callers
  - `usage.py` — usage accounting for a completed llm-node call, over the
    canonical emitter in `base/lm/usage.py`

This door imports neither: it stays a dependency-free schema leaf (langchain
only), which is what lets the understanding consumer bind the same schema without
the rest of the kernel.
"""

from __future__ import annotations

from langchain_core.tools import tool


# wire name = function name `execute_code` (snake_case), matching the form
# used in the system prompt (`execute_code(code: str)`) and the broader
# tool-naming convention across providers. PEP 8 prefers PascalCase for
# classes; with @tool we're declaring a tool schema by function, so the
# function naming rules apply. Body is unused — bind_tools only consumes
# the wrapped tool's schema (name + docstring + arg types).
@tool("execute_code", parse_docstring=True)
def execute_code(code: str) -> str:
    """Run a Python snippet in your sandbox.

    Each call runs in an ephemeral interpreter. Variables do not persist between calls.

    Merged stdout/stderr comes back as the tool result. A hard wall-clock
    timeout raises TimeoutError if the snippet runs too long; long output
    keeps its head and tail (the middle is elided) and the full text is
    written to a file under `.exec_output/` in your workspace.

    The `ava` module is NOT auto-imported — include ``import ava`` to use the SDK.

    Args:
        code: Python source code to run.
    """
    raise NotImplementedError("schema-only — actual execution dispatched by exec_node")
