"""Explicit checkpoint resources passed through one hosted admission."""

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from base.agents.history.delta_read_compat import RecoveryReconstructionScope


@dataclass(frozen=True)
class TurnCheckpoints:
    """A scoped read view over the host's original saver and compiled graph."""

    saver: AsyncPostgresSaver
    graph: CompiledStateGraph[Any, Any, Any, Any]
    reconstruction: RecoveryReconstructionScope | None = None

    def bind(self, scope: RecoveryReconstructionScope | None) -> "TurnCheckpoints":
        if scope is None:
            return self
        reader = scope.reader()
        # LangGraph's public copy preserves nodes, channels, compile options and
        # serializer allowlists. Only this admission's checkpointer changes.
        return TurnCheckpoints(reader, self.graph.copy({"checkpointer": reader}), scope)
