"""ava_builtins.plugins.ava_fleet integration.

Two layers:
- plugin surface end-to-end (`install` of its `contribute()`): installs the
  `ava.self.set_label` self member + the `ava.ui.notify` /
  `edit_notice` / `dismiss_notice` push members + a system-prompt section.
- `set_label()` writes the agent's own label (sticky, so the
  labeler won't overwrite it) — reflected in the agent snapshot (the single
  source the monitoring view reads). `notify()` / `edit_notice()` /
  `dismiss_notice()` write and read the unified agent_notices queue
  (migration 0053). At most one notice is open per agent (notify auto-resolves
  the previous one), so edit/dismiss take no id.
"""

import importlib
import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

import ava
import ava.agents
from agent.graph.prompt.system_prompt import build_system_prompt
from ava.sdk_surface import install
from ava.sdk_surface.installation import installed
from ava_builtins.plugins.ava_fleet.tests.registry_support import (
    fleet_registry,
    installed_fleet_surface,
    set_fleet_configuration,
)
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY
from tests.fixtures.pin_agent import pin_agent


def _fleet_slices(**changes: Any) -> AgentSlices:
    installation = installed()
    assert installation is not None
    return AgentSlices.resolve(
        plugin_configs=installation.configs,
        default_reader=defaults(**changes),
    )


@pytest.fixture
def load_activity_plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install Fleet with a valid local sampling policy; uninstall after the test."""
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    with installed_fleet_surface():
        yield


def test_plugin_registers_self_members(load_activity_plugin: None):
    for name in ("set_label",):
        assert callable(getattr(ava.self, name))
        assert name in ava.self.__all_for_ava__


def test_member_torn_down_on_uninstall(load_activity_plugin: None):
    # Uninstall must remove the member from both the module and its __all_for_ava__.
    install.uninstall()
    assert not hasattr(ava.self, "set_label")
    assert "set_label" not in ava.self.__all_for_ava__


def test_plugin_registers_prompt_section(load_activity_plugin: None):
    prompt = build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )
    assert "ava.self.set_label" in prompt


def test_prompt_assigns_shared_milestone_reporting(load_activity_plugin: None):
    """The rendered prompt carries the reporting contract with the plugin."""
    prompt = build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )

    assert prompt.count("one reporter per milestone") == 1
    assert "directly to whoever must act" in prompt
    assert "do not relay unchanged results" in prompt
    assert "actionable updates" in prompt
    assert "duplicate task-log writes" in prompt


def test_enabled_fleet_preserves_parallel_work_policy(load_activity_plugin: None):
    """Fleet preserves core parallel guidance without requiring task tracking."""
    prompt = build_system_prompt(
        fleet_registry(),
        _fleet_slices(agent__prompt_delegation_check_enabled=True),
        agent_id=1,
        catalog=build_model_catalog(),
    )
    assert "Workflow selection belongs to `ava-workflow`" in prompt
    assert "enabling Fleet does not require a registry task or a management tree" in prompt
    assert prompt.count("Proactively parallelize independent work.") == 1
    assert "Fork when your existing context is valuable to the task" in prompt
    assert "otherwise spawn fresh or reuse a suitable, available agent." in prompt
    assert "Before using Fleet coordination or task tracking" in prompt
    assert "accepted assignments determine reporting" in prompt
    assert "With no delegator, deliver directly" in prompt
    for instruction in (
        "Doing everything yourself is the fallback",
        "spawn one agent per part",
        "delegation is the expected pattern",
        "When the task registry is available and a noticed signal",
        "Progress and conclusions go to your manager",
        "enabling Fleet does not require delegation",
        "parallelizable work does not require delegation",
    ):
        assert instruction not in prompt


def test_fleet_does_not_duplicate_core_lifecycle(load_activity_plugin: None):
    """Fleet adds collaboration guidance without owning the core lifecycle."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import fleet_self_section

    section = fleet_self_section(_fleet_slices(), catalog=build_model_catalog())
    prompt = build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )
    assert "# Efficient long-running operation" not in section
    assert prompt.count("# Efficient long-running operation") == 1
    assert "do not plan to terminate it yourself" not in section
    assert "operating contract" in section


def test_peer_communication_survives_human_guidance_toggle(
    load_activity_plugin: None,
    monkeypatch: pytest.MonkeyPatch,
):
    """Turning off human interruption guidance must not remove peer discipline."""

    set_fleet_configuration(reduce_context_switch=False)
    prompt = build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )
    assert prompt.count("## Agent-to-agent communication") == 1
    assert "## Reduce context switch for the human" not in prompt
    assert "explicit reporting agreements still apply" in prompt
    assert "protocol receipt ACKs" not in prompt
    assert "Periodic checking does not imply periodic broadcasting" in prompt
    assert "without courtesy ACKs" in prompt


def test_prompt_section_dismiss_notice_after_dialog_reply(load_activity_plugin: None):
    """When the user has already answered in the dialog, the agent must
    actively dismiss the pending notice instead of leaving it open — and the
    rule is phrased semantically (no dismiss_notice call name)."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import fleet_self_section

    section = fleet_self_section(_fleet_slices(), catalog=build_model_catalog())
    assert "Dismiss a pending notice" in section
    assert "when the dialog resolves it" in section
    assert "dismiss_notice" not in section


def test_prompt_section_queue_delivery_mandate(load_activity_plugin: None):
    """Keep asynchronous delivery and resolved-notice semantics resident."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import fleet_self_section

    section = fleet_self_section(_fleet_slices(), catalog=build_model_catalog())
    assert "queue necessary decisions and results" in section
    assert "even while they are offline" in section
    assert "offline" in section
    assert "numbered notice" not in section
    assert "Posting is delivery" in section
    assert "reduce-context-switch" not in section


def test_prompt_section_reduce_context_switch_gating(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """The platform reduce-context-switch default renders only while the
    FleetConfig.reduce_context_switch toggle is on; off is the escape hatch
    back to the pre-platform behavior (empty section)."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import (
        reduce_context_switch_section,
    )

    set_fleet_configuration(reduce_context_switch=True)
    assert "Queue, never push" in reduce_context_switch_section(
        _fleet_slices(), catalog=build_model_catalog()
    )

    set_fleet_configuration(reduce_context_switch=False)
    assert reduce_context_switch_section(_fleet_slices(), catalog=build_model_catalog()) == ""


def test_prompt_section_reduce_context_switch_content(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """Keep interruption limits resident and route procedures to the skill."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import (
        reduce_context_switch_section,
    )

    set_fleet_configuration(reduce_context_switch=True)
    section = reduce_context_switch_section(_fleet_slices(), catalog=build_model_catalog())

    assert "Queue, never push" in section
    assert "irreversible risk in motion" in section
    assert "lack of acknowledgment does not justify escalation" in section
    assert "One notice per agent, updated in place" in section
    assert "reporting cadence, aggregation" in section
    assert "Milestones, not motion" not in section
    assert "reduce-context-switch-for-human" in section
    assert "explicit request to be woken" in section
    assert "With no delegator, deliver directly" not in section


def test_reduce_context_switch_reaches_the_prompt(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """End to end: the toggle gates the section's presence in the assembled
    system prompt."""

    section = "## Reduce context switch for the human"
    set_fleet_configuration(reduce_context_switch=True)
    assert section in build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )

    set_fleet_configuration(reduce_context_switch=False)
    assert section not in build_system_prompt(
        fleet_registry(), _fleet_slices(), agent_id=1, catalog=build_model_catalog()
    )


def test_fleet_operating_contract_is_loaded_on_demand(load_activity_plugin: None):
    """The prompt routes chosen capabilities to complete, preserved procedures."""
    from ava_builtins.plugins.ava_fleet import agent_runtime

    section = agent_runtime.fleet_self_section(_fleet_slices(), catalog=build_model_catalog())
    skill_directory = Path(agent_runtime.__file__).parent / "skills" / "ava-fleet"
    skill = (skill_directory / "SKILL.md").read_text()
    contract = (skill_directory / "reference" / "operating-contract.md").read_text()
    assert "load `ava-fleet` and its applicable operating contract" in section
    assert "reference/operating-contract.md" in skill
    for obligation in (
        "If you choose Fleet task tracking",
        "parent's active children",
        "one business delivery to the current delegator",
        "`created_by` is an audit trail, not a routing field",
        "no automatic notification to the creator",
        "reserve `ava.tasks.create_and_assign` for when the owner must be spawned",
        "do not plan to terminate it yourself",
        "When you accept delegated work",
        "One reporter per milestone",
        "When the user approves your plan or tells you to start",
    ):
        assert obligation in contract
        assert obligation not in section
    assert "## Fleet task interaction" not in section
    assert "ava.tasks.create" not in section


def test_fleet_contract_preserves_numeric_identifier_prefixes():
    """Identifier formatting belongs to the opt-in operating contract."""
    from ava_builtins.plugins.ava_fleet import agent_runtime

    contract = (
        Path(agent_runtime.__file__).parent
        / "skills"
        / "ava-fleet"
        / "reference"
        / "operating-contract.md"
    ).read_text()
    for identifier in ("Ava #<id>", "task #<id>", "PR #<id>"):
        assert identifier in contract
    assert "A bare number is ambiguous" in contract


def test_task_conversion_absent_when_plugin_disabled(**changes: Any):
    """Prompt copy and the task SDK reference disappear together with the
    fleet plugin."""
    prompt = build_system_prompt(
        EMPTY,
        AgentSlices.resolve(
            default_reader=defaults(**changes),
        ),
        agent_id=1,
        catalog=build_model_catalog(),
    )

    assert "## Fleet task interaction" not in prompt
    assert "create directly with `ava.tasks.create`" not in prompt
    assert "ava.tasks.create" not in prompt
    assert "One reporter per milestone" not in prompt


def test_spawn_label_param_gated_on_plugin(load_activity_plugin: None):
    # With the plugin enabled, spawn is wrapped and exposes the `label` arg. The
    # wrap fact lives in the introspectable stack (the chained callable mimics
    # the original's __module__), and the added `label` shows in the signature.
    assert [p for p, _ in ava.extend.stack("agents.spawn")] == ["ava_fleet"]
    assert "label" in inspect.signature(ava.agents.spawn).parameters


def test_core_spawn_has_no_label_arg():
    # Without the plugin (plain core spawn), there is no `label` arg —
    # the labeler auto-names new agents. Reload to guarantee an unwrapped state.
    importlib.reload(ava.agents)
    assert ava.agents.spawn.__module__ == "ava.agents"
    assert "label" not in inspect.signature(ava.agents.spawn).parameters


def _label_of(db_conn: psycopg.Connection, agent_id: int) -> str | None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id=%s", (agent_id,))
        row = cur.fetchone()
    return None if row is None else row[0]


def test_set_label_hands_the_agents_own_label_to_the_gateway(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    from ava import gateway_client

    calls: list[tuple[int, str, str]] = []

    def patch_label(agent_id: int, label: str, *, source: str = "self") -> None:
        calls.append((agent_id, label, source))

    monkeypatch.setattr(gateway_client, "patch_label", patch_label)
    pin_agent(7)
    ava.self.set_label("auth-refactor lead")  # type: ignore[attr-defined]
    # Empty string clears back to the default (#N fallback).
    ava.self.set_label("")  # type: ignore[attr-defined]

    assert calls == [(7, "auth-refactor lead", "self"), (7, "", "self")]


def test_plugin_registers_ui_notice_members(load_activity_plugin: None):
    # The whole notice surface is a push to the user's queue — registered on the
    # ui namespace (not self), gated on this plugin: it depends on a human
    # supervising.
    for name in ("notify", "edit_notice", "dismiss_notice"):
        assert callable(getattr(ava.ui, name))  # type: ignore[attr-defined]
        assert name in ava.ui.__all_for_ava__


def test_notify_validates_title_priority_and_blocking(load_activity_plugin: None):
    with pytest.raises(ValueError, match="title"):
        ava.ui.notify("   ", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="priority"):
        ava.ui.notify("ok", priority="P9", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    # blocking is a strict subset of require_response: an FYI can never stall you.
    with pytest.raises(ValueError, match="require_response"):
        ava.ui.notify("ok", blocking=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]


def test_fleet_spawn_preserves_caller_creation_key(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> int:
        captured.append(kwargs)
        return 42

    monkeypatch.setattr(ava.gateway_client, "spawn", capture)
    assert ava.agents.spawn(prompt="one goal", idempotency_key="fleet-birth") == 42
    assert captured[0]["idempotency_key"] == "fleet-birth"
    assert captured[0]["label"] is None
    assert "idempotency_key" in inspect.signature(ava.agents.spawn).parameters


def test_fleet_spawn_forwards_keyed_fork_without_mode_flag(
    load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> int:
        captured.append(kwargs)
        return 42

    monkeypatch.setattr(ava.gateway_client, "spawn", capture)
    assert ava.agents.spawn(fork_from=1, idempotency_key="fork-intent") == 42
    assert captured[0]["fork_from"] == 1
    assert captured[0]["idempotency_key"] == "fork-intent"
    assert "require_idempotency" not in inspect.signature(ava.agents.spawn).parameters
    obsolete: dict[str, Any] = {"idempotency_key": "intent", "require_idempotency": True}
    with pytest.raises(TypeError, match="require_idempotency"):
        ava.agents.spawn(**obsolete)
    assert len(captured) == 1
