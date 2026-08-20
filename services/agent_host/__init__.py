"""Agent host — the hosted-mode runner that runs agents' turns as tasks.

Phase 1 of `future/infra/agent-runner-as-server.md`. Only the wake dispatcher
lives here so far (`dispatcher.py`); the host service that supplies its
`run_turn` callable lands with the service spec.
"""
