"""Reader provenance contracts for the profile consumption matrix."""

from pathlib import Path

import pytest

from cli.commands.lifecycle.tests.startup.config_read_facts import explicit_config_reads


@pytest.mark.parametrize(
    "source",
    [
        "from ava.sdk_surface.settings import config_authority as owner\n"
        "owner().service_field_value('web_fetch_model')",
        "import ava.sdk_surface.settings as module\n"
        "module.config_authority().service_field_value(name='web_fetch_model')",
        "from base.config.service_read import ConfigAuthority as Authority\n"
        "def consume(authority: Authority):\n"
        "    authority.service_field_value('web_fetch_model')",
    ],
)
def test_authority_reads_use_the_canonical_field_domain(source: str) -> None:
    assert explicit_config_reads(source) == [("web", "web_fetch_model")]


@pytest.mark.parametrize(
    "source",
    [
        "from base.agents.context import AvaContext as Context\n"
        "def consume(context: Context):\n"
        "    context.require_agent().read('display', 'timeline_default_limit')",
        "import base.agents.context as module\n"
        "from langgraph.runtime import Runtime as GraphRuntime\n"
        "def consume(graph: GraphRuntime[module.AvaContext]):\n"
        "    ctx = graph.context\n"
        "    reader = ctx.require_agent()\n"
        "    return lambda: reader.read(domain='display', field='timeline_default_limit')",
        "from base.host.env.agent_slices import AgentSlices\n"
        "def consume(agent: 'AgentSlices', field: str):\n"
        "    agent.read('display', field)",
    ],
)
def test_agent_reads_require_a_proven_receiver(source: str) -> None:
    reads = explicit_config_reads(source)
    assert len(reads) == 1
    assert reads[0][0] == "display"


@pytest.mark.parametrize(
    "source",
    [
        "def consume(config_authority):\n"
        "    config_authority().service_field_value('web_fetch_model')",
        "from unrelated import config_authority\n"
        "config_authority().service_field_value('web_fetch_model')",
        "from ava.sdk_surface.settings import config_authority\n"
        "def consume(config_authority):\n"
        "    config_authority().service_field_value('web_fetch_model')",
        "from ava.sdk_surface.settings import config_authority\n"
        "config_authority = unrelated\n"
        "config_authority().service_field_value('web_fetch_model')",
        "from base.agents.context import AvaContext\n"
        "def consume(ctx: AvaContext):\n"
        "    ctx = unrelated\n"
        "    ctx.require_agent().read('display', 'timeline_default_limit')",
        "from unrelated import AvaContext\n"
        "def consume(ctx: AvaContext):\n"
        "    ctx.require_agent().read('display', 'timeline_default_limit')",
        "from base.agents.context import AvaContext\n"
        "def consume(ctx: AvaContext):\n"
        "    return lambda ctx: ctx.require_agent().read('display', 'timeline_default_limit')",
        "from base.agents.context import AvaContext\n"
        "def consume(ctx: AvaContext):\n"
        "    ctx.require_agent = unrelated\n"
        "    ctx.require_agent().read('display', 'timeline_default_limit')",
        "from langgraph.runtime import Runtime\n"
        "from base.agents.context import AvaContext\n"
        "def consume(graph: Runtime[AvaContext]):\n"
        "    graph.context = unrelated\n"
        "    graph.context.require_agent().read('display', 'timeline_default_limit')",
    ],
)
def test_unrelated_or_rebound_readers_do_not_supply_domain_facts(source: str) -> None:
    assert explicit_config_reads(source) == []


@pytest.mark.parametrize(
    "expression",
    [
        "authority().service_field_value('undeclared_field')",
        "ctx.require_agent().read('display', 'web_fetch_model')",
        "ctx.require_agent().read('undeclared_domain', field)",
    ],
)
def test_proven_readers_reject_invalid_literal_contracts(expression: str) -> None:
    source = (
        "from ava.sdk_surface.settings import config_authority as authority\n"
        "from base.agents.context import AvaContext\n"
        "def consume(ctx: AvaContext, field: str):\n"
        f"    {expression}\n"
    )
    with pytest.raises(AssertionError, match="undeclared"):
        explicit_config_reads(source)


def test_real_sdk_and_agent_consumers_supply_display_and_web() -> None:
    root = Path(__file__).resolve().parents[5]
    for path, domain in (
        ("ava/web.py", "web"),
        ("ava/agents/__init__.py", "display"),
        ("ava/shell/sessions.py", "display"),
        ("agent/graph/_init_context.py", "display"),
        ("agent/graph/claim/_present.py", "display"),
        ("agent/graph/claim/node.py", "display"),
        ("agent/graph/exec/node.py", "display"),
        ("agent/graph/llm/node.py", "display"),
        ("agent/hooks/_registry.py", "display"),
    ):
        assert domain in {
            owner for owner, _field in explicit_config_reads((root / path).read_text(), path)
        }, path


@pytest.mark.parametrize(
    "source",
    [
        "from base.config import ConfigBoot as Boot\n"
        "config = Boot(profile='runner')\n"
        "view = config.view\n"
        "consume = lambda: view.sandbox.mcp_connect_timeout_seconds",
        "import base.config as module\n"
        "module.ConfigBoot().view.sandbox.mcp_connect_timeout_seconds",
        "from base.config import ConfigBoot\n"
        "def consume(owner: ConfigBoot):\n"
        "    owner.view.sandbox.mcp_connect_timeout_seconds",
    ],
)
def test_boot_view_reads_require_the_canonical_instance(source: str) -> None:
    assert explicit_config_reads(source) == [("sandbox", "mcp_connect_timeout_seconds")]


@pytest.mark.parametrize(
    "source",
    [
        "from unrelated import ConfigBoot\nConfigBoot().view.sandbox.mcp_connect_timeout_seconds",
        "from base.config import ConfigBoot\n"
        "def consume(ConfigBoot):\n"
        "    ConfigBoot().view.sandbox.mcp_connect_timeout_seconds",
        "from base.config import ConfigBoot\n"
        "config = ConfigBoot()\n"
        "config = unrelated\n"
        "config.view.sandbox.mcp_connect_timeout_seconds",
        "from base.config import ConfigBoot\n"
        "config = ConfigBoot()\n"
        "config.view = unrelated\n"
        "config.view.sandbox.mcp_connect_timeout_seconds",
        "def consume(config):\n    config.view.sandbox.mcp_connect_timeout_seconds",
    ],
)
def test_unrelated_boot_views_do_not_supply_domain_facts(source: str) -> None:
    assert explicit_config_reads(source) == []


@pytest.mark.parametrize("read", ["web.mcp_connect_timeout_seconds", "sandbox.undeclared_field"])
def test_boot_view_rejects_unknown_and_mismatched_fields(read: str) -> None:
    with pytest.raises(AssertionError, match="undeclared"):
        explicit_config_reads(f"from base.config import ConfigBoot\nConfigBoot().view.{read}")


def test_real_runner_boot_view_supplies_sandbox() -> None:
    root = Path(__file__).resolve().parents[5]
    for path in (
        "services/desktop/browser/mcp_daemon.py",
        "services/desktop/browser/mcp_wrapper.py",
        "services/desktop/computer/mcp_wrapper.py",
    ):
        assert ("sandbox", "mcp_connect_timeout_seconds") in explicit_config_reads(
            (root / path).read_text(), path
        )


def test_boot_view_accepts_declared_model_properties() -> None:
    field = "is_remote"
    assert explicit_config_reads(
        f"from base.config import ConfigBoot\nConfigBoot().view.data_plane.{field}"
    ) == [("data_plane", field)]


def test_boot_view_domain_guard_requires_the_same_proven_owner_and_true_branch() -> None:
    source = (
        "from base.config import ConfigBoot\n"
        "first = ConfigBoot()\n"
        "second = ConfigBoot()\n"
        "view = first.view\n"
        "if view.has_domain('agent'):\n"
        "    first.view.agent.understanding_enabled\n"
        "    second.view.agent.understanding_enabled\n"
        "else:\n"
        "    first.view.agent.understanding_enabled\n"
        "if unrelated.has_domain('agent'):\n"
        "    first.view.agent.understanding_enabled\n"
    )
    assert explicit_config_reads(source) == [("agent", "understanding_enabled")] * 3
