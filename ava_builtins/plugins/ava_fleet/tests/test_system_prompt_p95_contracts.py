"""The fleet plugin's namespaces stay on demand under the p95 `sdk_expand_in_system_prompt` default.

Split out of `agent/graph/tests/prompt/test_system_prompt.py`: it installs the built-in
`ava_fleet` plugin (which owns `ava.tasks`), so it lives with the plugin.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent.graph.prompt.system_prompt import sdk_expand_section
from ava.sdk_surface import install
from ava.sdk_surface.installation import installed
from base.config import FIELD_INFOS, settings
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import ExtensionRegistry
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices


@pytest.fixture(autouse=True)
def _no_plugin_expansions() -> Iterator[None]:
    # Reason about the framework expand list alone: take any installed plugin surface out
    # so a leak from another test cannot prepend stray paths, and put it back after.
    prior = installed()
    install.uninstall()
    yield
    install.uninstall()
    if prior is not None:
        install.install(
            prior.registry,
            catalog=prior.catalog,
            authority=prior.authority,
        )


def test_p95_contracts_leave_rare_namespaces_on_demand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    from ava_builtins.plugins.ava_fleet import plugin as fleet
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    factory = FIELD_INFOS["sdk_expand_in_system_prompt"].default_factory
    assert factory is not None
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", factory())
    install.install(
        ExtensionRegistry((("ava_fleet", fleet.contribute()),)),
        catalog=model_catalog,
        authority=config_authority,
    )

    text = sdk_expand_section(
        AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=model_catalog,
    )

    for module in ["shell", "files", "agents", "tasks"]:
        assert f"## ava.{module}\n" in text
    for module in ["memory", "self", "ui", "watcher", "web", "shell.sessions"]:
        assert f"## ava.{module}\n" not in text
    import ava

    # A module omitted from the standing reference remains available.
    ava.help(ava.watcher)
    assert "def launch(" in capsys.readouterr().out
    ava.help(ava.shell.sessions)
    assert "## ava.shell.sessions" in capsys.readouterr().out
