"""The graph runs every node on the state class built from the registry — not on what a node's
first-parameter annotation names (the static base class, which has none of the plugins' channels)."""

from pydantic import BaseModel

import agent.state as agent_state
from agent.graph import build_graph
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


class _Counter(BaseModel):
    seen: int = 0


def test_every_node_receives_the_plugins_state_channels() -> None:
    registry = ExtensionRegistry((("demo", PluginContributions(state=(_Counter,))),))

    graph = build_graph(None, registry)

    state_cls = graph.builder.state_schema
    assert state_cls is not agent_state.BaseAgentState
    assert "demo__seen" in state_cls.model_fields
    assert graph.builder.nodes, "the graph has nodes"
    for name, spec in graph.builder.nodes.items():
        assert spec.input_schema is state_cls, name
    # Building it rebinds nothing: the static name stays the base class.
    assert agent_state.AgentState is agent_state.BaseAgentState
