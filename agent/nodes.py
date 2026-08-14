"""Graph node name constants + Literal type.

Centralizes all legal node names — typo-safe (pyright catches) + refactor-
friendly (rename one place changes everywhere). Command(goto=...) also uses
generic narrowing to this Literal, letting the type checker catch illegal
goto strings.

Lives at `agent/nodes.py`, not under `agent/graph/`: `agent.hooks._registry`
needs NodeName at module top level (LangGraph resolves the hook runner's type
hints against module globals), and importing anything under `agent.graph`
runs the package __init__ → `_build` → `agent.hooks` — a cycle whenever
`agent.hooks` loads first. `agent/__init__` is deliberately import-light, so
this home is reachable from both sides. `agent/graph/_nodes.py` remains as a
re-export shim for existing callers (same pattern as `agent/graph/_context.py`).

Usage:
    from agent.nodes import CLAIM, BEFORE_LLM, NodeName
    return Command[NodeName](update={...}, goto=BEFORE_LLM)
"""

from typing import Literal

# Each constant narrowed to its own Literal — lets narrow goto types like
# `Command[Literal["before_exec", "after_exec"]]` also accept
# `goto=BEFORE_EXEC`. Annotating constants with the broader `NodeName` would
# cause narrow Command to report type mismatch (NodeName includes other
# Literal values not in the narrow set).
CLAIM: Literal["claim"] = "claim"
BEFORE_LLM: Literal["before_llm"] = "before_llm"
LLM: Literal["llm"] = "llm"
BEFORE_EXEC: Literal["before_exec"] = "before_exec"
EXEC: Literal["exec"] = "exec"
AFTER_EXEC: Literal["after_exec"] = "after_exec"
AFTER_INIT: Literal["after_init"] = "after_init"
INIT_CONTEXT: Literal["init_context"] = "init_context"

# LangGraph's terminal target. Spelled out rather than imported: langgraph's
# `END = sys.intern("__end__")` is inferred as `LiteralString`, which a narrow
# `Command[...]` type argument will not accept.
END: Literal["__end__"] = "__end__"

# Every legal `Command(goto=...)` target. END belongs here with the rest — it is
# a node in the graph as far as routing is concerned, and any node or hook may
# route to it (a terminate co-batched with other work does exactly that).
NodeName = Literal[
    "claim",
    "before_llm",
    "llm",
    "before_exec",
    "exec",
    "after_exec",
    "after_init",
    "init_context",
    "__end__",
]
