"""Child process for the eval-isolation boot tests: install the memory plugin, then apply boot.

Usage: `child_eval_isolation.py <isolated|network_allowlist|off>`; prints `ok` on success.
"""

import os
import sys
from types import SimpleNamespace

import meiva
from agent.process_boot import apply_per_agent_eval_isolation
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.config import settings
from base.packages.plugins.extensions import ExtensionRegistry
from meiva.sdk_surface import install
from meiva_builtins.plugins.meiva_memory import plugin


def _apply() -> None:
    apply_per_agent_eval_isolation(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )


def isolated() -> None:
    install.install(ExtensionRegistry((("meiva_memory", plugin.contribute()),)))
    meiva.tasks = SimpleNamespace(list=list)  # pyright: ignore[reportAttributeAccessIssue]
    meiva.__all_for_meiva__.append("tasks")

    original_path_doc = meiva.memory.PATH.__doc__
    os.environ["MEIVA_AGENT_ID"] = "417"
    meiva.context = MeivaContext(identity=AgentIdentity(417, True))
    settings.agent.eval_isolation = True
    settings.agent.eval_network_allowlist = []
    _apply()

    assert not hasattr(meiva, "web")
    assert not hasattr(meiva, "understand")
    assert not hasattr(meiva, "mcps")
    assert not hasattr(meiva, "ui")
    assert not hasattr(meiva.agents, "get_last_message")
    assert not hasattr(meiva, "tasks")
    assert meiva.memory.PATH.name == "memory-pool"
    assert meiva.memory.PATH.parent.name == "417"
    assert meiva.memory.PATH.is_dir()
    assert meiva.memory.PATH.__doc__ == original_path_doc
    assert meiva.memory.search("shared result") == []
    assert not hasattr(meiva.memory, "search_detailed")
    print("ok")


def network_allowlist() -> None:
    install.install(ExtensionRegistry((("meiva_memory", plugin.contribute()),)))
    os.environ["MEIVA_AGENT_ID"] = "418"
    meiva.context = MeivaContext(identity=AgentIdentity(418, True))
    settings.agent.eval_isolation = True
    settings.agent.eval_network_allowlist = ["web"]
    _apply()

    assert hasattr(meiva, "web")
    assert not hasattr(meiva, "understand")
    print("ok")


def off() -> None:
    install.install(ExtensionRegistry((("meiva_memory", plugin.contribute()),)))
    original_path = meiva.memory.PATH
    settings.agent.eval_isolation = False
    _apply()

    assert hasattr(meiva, "web")
    assert hasattr(meiva, "understand")
    assert hasattr(meiva, "mcps")
    assert hasattr(meiva, "ui")
    assert hasattr(meiva.agents, "get_last_message")
    assert original_path == meiva.memory.PATH
    print("ok")


{"isolated": isolated, "network_allowlist": network_allowlist, "off": off}[sys.argv[1]]()
