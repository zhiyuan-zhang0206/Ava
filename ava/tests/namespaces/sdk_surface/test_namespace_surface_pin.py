"""The names each core `ava` namespace shows to the agent do not change by accident.

`help()`, SDK-expand and the metering wrappers all read the same list
(`agent_visible_names`). A helper that moves between modules, or a submodule that gains a
public name, must not widen what an agent sees. These literals are the surface as reviewed; a
deliberate change to a namespace edits its list here in the same PR.
"""

from __future__ import annotations

import pytest

import ava
from ava.sdk_surface.discovery import agent_visible_names

_SURFACE: dict[str, list[str]] = {
    "agents": [
        "AgentDirectoryPage",
        "AgentRow",
        "AgentStatus",
        "CommandInfo",
        "Machine",
        "Neighbor",
        "OpenTaskRow",
        "OpenTasksHint",
        "RestartResult",
        "ResurrectResult",
        "ShellSessionKillTiming",
        "ShellSessionsKill",
        "TerminateOutcome",
        "TerminateResult",
        "commands",
        "compaction",
        "get_ancestors",
        "get_last_message",
        "get_launch_attempt",
        "get_neighbors",
        "get_status",
        "list_agents",
        "list_machines",
        "presets",
        "restart",
        "retry_launch",
        "resurrect",
        "send_message",
        "spawn",
        "terminate",
        "work",
    ],
    "self": [
        "AGENT_ID",
        "MACHINE_SPEC",
        "MachineSpec",
        "attach",
        "compact",
        "pause_heartbeat",
        "restart",
        "terminate",
    ],
    "ui": ["Page", "close", "serve", "show"],
    "watcher": ["at", "cron", "launch"],
    "web": ["ReasoningEffort", "SearchResult", "fetch", "search"],
    "skills": [],
}


@pytest.mark.parametrize("namespace", sorted(_SURFACE))
def test_the_agent_visible_names_of_a_core_namespace_are_pinned(namespace: str) -> None:
    assert agent_visible_names(getattr(ava, namespace)) == _SURFACE[namespace]


def test_the_mcps_surface_is_its_two_helpers_plus_the_configured_servers() -> None:
    """The servers come from the machine's mcp.json and the plugins' bundled .mcp.json, so the
    list is live; what is pinned is that nothing else is in it."""
    from ava import mcps

    names = agent_visible_names(mcps)
    assert {"servers", "description"} <= set(names)
    assert set(names) - {"servers", "description"} <= set(mcps.servers())
