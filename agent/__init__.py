"""Ava agent kernel — LangGraph self-cycling graph + thin loop launcher.

Separate from the `ava` SDK package: the SDK is the layer agent code (in the
subprocess) *sees*, this package is the layer that *hosts* the agent.
`agent/loop.py` degenerates into a thin launcher (one graph.ainvoke per
turn, driven by `agent/_runloop.py` until claim requests process exit),
all dispatch / cancel logic lives in graph nodes (claim node
pulls inbound + dispatches by kind; llm/exec node uses RAII cancel; exec
spawns subprocess to execute agent code).

Plans that belong to this package: `agent/prompt-architecture.md` (prompt
architecture) and `agent/import-existing-agent-history.md` (onboarding import).

This `__init__` stays deliberately import-light: `python -m agent` imports the
package before running `agent/__main__.py`, and `__main__.py` claims the row
*before* the heavy `from .loop import run`. Eagerly importing `.loop` here would
pull the langgraph/SDK chain into that pre-claim window and defeat the early
claim. Import the launcher from its submodule instead (`from agent.loop import
main`).
"""
