"""Real borrowed-identity attachment harness shared by owner contract tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import ava
from agent import state as state_module
from ava import external
from ava.sdk_surface.install import Installation
from base.db import Database
from base.packages.plugins.extensions import EMPTY
from tests.fixtures.pin_agent import pin_agent

# A bounded handshake allows close telemetry to drain on a loaded runner.
HANDSHAKE_BOUND_S = 30.0


def _union(left: set[str], right: set[str]) -> set[str]:
    return left | right


class ExamplePlugin(BaseModel):
    seen: Annotated[set[str], _union] = Field(default_factory=set)


class ExampleMessagesPlugin(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list[AnyMessage])


class ExampleState(state_module.BaseAgentState):
    """What `build_agent_state` makes for a registry whose `sample` plugin declares `ExamplePlugin`
    and whose messages-declaring plugin declares `ExampleMessagesPlugin`."""

    sample__seen: Annotated[set[str], _union] = Field(default_factory=set)
    __plugin_base_declared__ = frozenset({"messages"})
    __plugin_state_classes__ = frozenset({ExamplePlugin, ExampleMessagesPlugin})


@pytest.fixture
def attached_runtime(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    lease: dict[str, Any] = {
        "id": "lease",
        "session_id": 0,
        "agent_id": 405,
        "machine": "local-runner",
        "status": "active",
        "delta_version": 0,
        "applied_version": 0,
        "plugin_delta": [],
        "automatic": False,
        "event_delivery_protocol_version": None,
    }
    staged: list[dict[str, Any]] = []
    snapshot = ExampleState(sample__seen={"native"})
    pin_agent(None, owns_loop=True)
    ava.unbind_exec_turn()
    request.addfinalizer(ava.unbind_exec_turn)

    installation = Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=True,
        undo=(),
    )
    monkeypatch.setattr(ava, "__plugin_installation__", installation, raising=False)

    def loader_stub(**_kwargs: object) -> None:
        """Accept the `surface` kwarg attach passes (ignored)."""

    monkeypatch.setattr(ava, "ensure_plugins_loaded", loader_stub)
    monkeypatch.setattr(external, "machine_name", lambda: "local-runner")

    def load(_agent_id: int) -> tuple[ExampleState, dict[str, Any], None]:
        return snapshot.model_copy(deep=True), {"llm_model": "external-test"}, None

    monkeypatch.setattr(external, "load_snapshot", load)

    def require(_db: Database, lease_id: str, attesting: dict[str, Any]) -> dict[str, Any]:
        assert lease_id == "lease"
        assert attesting == {"pid": 777}
        if lease["status"] != "active":
            raise RuntimeError("lease expired")
        return dict(lease)

    def stage(
        db: Database,
        lease_id: str,
        attesting: dict[str, Any],
        delta: dict[str, Any],
        *,
        expected_version: int,
    ) -> None:
        require(db, lease_id, attesting)
        if expected_version != lease["delta_version"]:
            raise RuntimeError("stale version")
        staged.append(delta)
        lease["plugin_delta"].append(delta)
        lease["delta_version"] += 1

    monkeypatch.setattr(external.control, "require_active", require)
    monkeypatch.setattr(external.control, "merge_plugin_delta", stage)
    monkeypatch.setattr(external, "process_metadata", lambda: {"pid": 777})

    # This suite models the pre-event-log lease boundary with a symbolic lease
    # id. The receipt seam is integration-tested against real UUID leases;
    # keeping it outside this state-machine fixture avoids an accidental DB
    # dial that the fixture cannot represent.
    def no_local_participant(_db: object, *_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        "base.agents.impersonation.manifest.open_local_participant", no_local_participant
    )
    return lease, snapshot, staged


@pytest.fixture
def attached_mcp_runtime(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> Iterator[tuple[dict[str, Any], external.Attachment]]:
    """Provide the MCP caller's real borrowed identity and close its lifetime."""
    lease, _, _ = attached_runtime
    attachment = external.attach("lease")
    try:
        yield lease, attachment
    finally:
        attachment.close()
