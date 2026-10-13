"""The graph's own hooks and the node that runs a hook list.

The hook framework (`Hook`, `make_hook_runner`) lives in `agent.hooks`; the hooks that need
the graph kernel to do their work live here. `framework.py` lists the built-in hooks in run
order, `compact.py` is the compaction machinery and its reminder hook, `capabilities.py` the
capability-index drift check, `skill_recall.py` the skill recall hook, and `node.py` wraps one
hook list into a graph node with the same enter/exit lifecycle events every other node has.
The door is docstring-only.
"""
