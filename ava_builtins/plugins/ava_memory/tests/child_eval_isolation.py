"""Child process for the eval-isolation boot tests: install the memory plugin, then apply boot.

Usage: `child_eval_isolation.py <isolated|network_allowlist|off>`; prints `ok` on success.
"""

import os
import sys
from types import SimpleNamespace

import ava
from agent.process_boot import _apply_per_agent_eval_isolation
from ava.sdk_surface import install
from ava_builtins.plugins.ava_memory import plugin
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.config import settings
from base.packages.plugins.extensions import ExtensionRegistry


def _apply() -> None:
    _apply_per_agent_eval_isolation(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )


def isolated() -> None:
    install.install(ExtensionRegistry((("ava_memory", plugin.contribute()),)))
    ava.tasks = SimpleNamespace(list=list)  # pyright: ignore[reportAttributeAccessIssue]
    ava.__all_for_ava__.append("tasks")

    original_path_doc = ava.memory.PATH.__doc__
    os.environ["AVA_AGENT_ID"] = "417"
    ava.context = AvaContext(identity=AgentIdentity(417, True))
    settings.agent.eval_isolation = True
    settings.agent.eval_network_allowlist = []
    _apply()

    assert not hasattr(ava, "web")
    assert not hasattr(ava, "understand")
    assert not hasattr(ava, "mcps")
    assert not hasattr(ava, "ui")
    assert not hasattr(ava.agents, "get_last_message")
    assert not hasattr(ava, "tasks")
    assert ava.memory.PATH.name == "memory-pool"
    assert ava.memory.PATH.parent.name == "417"
    assert ava.memory.PATH.is_dir()
    assert ava.memory.PATH.__doc__ == original_path_doc
    assert ava.memory.search("shared result") == []
    assert not hasattr(ava.memory, "search_detailed")
    print("ok")


def network_allowlist() -> None:
    install.install(ExtensionRegistry((("ava_memory", plugin.contribute()),)))
    os.environ["AVA_AGENT_ID"] = "418"
    ava.context = AvaContext(identity=AgentIdentity(418, True))
    settings.agent.eval_isolation = True
    settings.agent.eval_network_allowlist = ["web"]
    _apply()

    assert hasattr(ava, "web")
    assert not hasattr(ava, "understand")
    print("ok")


def off() -> None:
    install.install(ExtensionRegistry((("ava_memory", plugin.contribute()),)))
    original_path = ava.memory.PATH
    settings.agent.eval_isolation = False
    _apply()

    assert hasattr(ava, "web")
    assert hasattr(ava, "understand")
    assert hasattr(ava, "mcps")
    assert hasattr(ava, "ui")
    assert hasattr(ava.agents, "get_last_message")
    assert original_path == ava.memory.PATH
    print("ok")


{"isolated": isolated, "network_allowlist": network_allowlist, "off": off}[sys.argv[1]]()
