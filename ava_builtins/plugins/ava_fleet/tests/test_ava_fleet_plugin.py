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
from ava_builtins.plugins.ava_fleet.tests.registry_support import (
    fleet_registry,
    installed_fleet_surface,
    set_fleet_configuration,
)
from base.agents.observation.snapshot import select_one
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import EMPTY
from tests.fixtures.pin_agent import pin_agent


def _seed_agent(db: psycopg.Connection) -> int:
    """Insert an agents + agents_meta row (log() / set_label() write against an
    existing meta row; the real spawn path always creates it first)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id: int = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (agent_id,),
        )
    db.commit()
    return agent_id


@pytest.fixture(autouse=True)
def _sdk_via_inprocess_gateway(monkeypatch: pytest.MonkeyPatch):
    """The notice SDK now goes through the unified gateway write API (R3 door
    ④): route the SDK's gateway client at the in-process app so notify /
    edit_notice / dismiss_notice hit the real endpoints against the test DB."""
    from fastapi.testclient import TestClient

    from ava.gateway_client.transport import use_client
    from gateway.app import app

    with TestClient(app, base_url="http://test-gateway") as tc, use_client(tc):
        yield


@pytest.fixture
def _load_activity_plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install Fleet with a valid local sampling policy; uninstall after the test."""
    from base.agents.sdk import call_policy

    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    with installed_fleet_surface():
        yield


def test_plugin_registers_self_members(_load_activity_plugin: None):
    for name in ("set_label",):
        assert callable(getattr(ava.self, name))
        assert name in ava.self.__all_for_ava__


def test_member_torn_down_on_uninstall(_load_activity_plugin: None):
    # Uninstall must remove the member from both the module and its __all_for_ava__.
    install.uninstall()
    assert not hasattr(ava.self, "set_label")
    assert "set_label" not in ava.self.__all_for_ava__


def test_plugin_registers_prompt_section(_load_activity_plugin: None):
    prompt = build_system_prompt(fleet_registry(), AgentSlices.resolve(), agent_id=1)
    assert "ava.self.set_label" in prompt


def test_prompt_assigns_shared_milestone_reporting(_load_activity_plugin: None):
    """The rendered prompt carries the reporting contract with the plugin."""
    prompt = build_system_prompt(fleet_registry(), AgentSlices.resolve(), agent_id=1)

    assert prompt.count("one reporter per milestone") == 1
    assert "directly to whoever must act" in prompt
    assert "do not relay unchanged results" in prompt
    assert "actionable updates" in prompt
    assert "duplicate task-log writes" in prompt


def test_enabled_fleet_preserves_workflow_choice(_load_activity_plugin: None):
    """Installing Fleet exposes capabilities without imposing a work strategy."""
    prompt = build_system_prompt(fleet_registry(), AgentSlices.resolve(), agent_id=1)
    assert "Workflow selection belongs to `ava-workflow`" in prompt
    assert (
        "enabling Fleet does not require delegation, a registry task, or a management tree"
        in prompt
    )
    assert "Before using Fleet coordination or task tracking" in prompt
    assert "accepted assignments determine reporting" in prompt
    assert "With no delegator, deliver directly" in prompt
    for instruction in (
        "Doing everything yourself is the fallback",
        "spawn one agent per part",
        "delegation is the expected pattern",
        "When the task registry is available and a noticed signal",
        "Progress and conclusions go to your manager",
    ):
        assert instruction not in prompt


def test_fleet_does_not_duplicate_core_lifecycle(_load_activity_plugin: None):
    """Fleet adds collaboration guidance without owning the core lifecycle."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import _fleet_self_section

    section = _fleet_self_section(AgentSlices.resolve())
    prompt = build_system_prompt(fleet_registry(), AgentSlices.resolve(), agent_id=1)
    assert "# Efficient long-running operation" not in section
    assert prompt.count("# Efficient long-running operation") == 1
    assert "do not plan to terminate it yourself" not in section
    assert "operating contract" in section


def test_peer_communication_survives_human_guidance_toggle(
    _load_activity_plugin: None,
    monkeypatch: pytest.MonkeyPatch,
):
    """Turning off human interruption guidance must not remove peer discipline."""

    set_fleet_configuration(reduce_context_switch=False)
    prompt = build_system_prompt(fleet_registry(), AgentSlices.resolve(), agent_id=1)
    assert prompt.count("## Agent-to-agent communication") == 1
    assert "## Reduce context switch for the human" not in prompt
    assert "explicit reporting agreements still apply" in prompt
    assert "protocol receipt ACKs" not in prompt
    assert "Periodic checking does not imply periodic broadcasting" in prompt
    assert "without courtesy ACKs" in prompt


def test_prompt_section_dismiss_notice_after_dialog_reply(_load_activity_plugin: None):
    """When the user has already answered in the dialog, the agent must
    actively dismiss the pending notice instead of leaving it open — and the
    rule is phrased semantically (no dismiss_notice call name)."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import _fleet_self_section

    section = _fleet_self_section(AgentSlices.resolve())
    assert "Dismiss a pending notice" in section
    assert "when the dialog resolves it" in section
    assert "dismiss_notice" not in section


def test_prompt_section_queue_delivery_mandate(_load_activity_plugin: None):
    """Keep asynchronous delivery and resolved-notice semantics resident."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import _fleet_self_section

    section = _fleet_self_section(AgentSlices.resolve())
    assert "queue necessary decisions and results" in section
    assert "even while they are offline" in section
    assert "offline" in section
    assert "numbered notice" not in section
    assert "Posting is delivery" in section
    assert "reduce-context-switch" not in section


def test_prompt_section_reduce_context_switch_gating(
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """The platform reduce-context-switch default renders only while the
    FleetConfig.reduce_context_switch toggle is on; off is the escape hatch
    back to the pre-platform behavior (empty section)."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import (
        _reduce_context_switch_section,
    )

    set_fleet_configuration(reduce_context_switch=True)
    assert "Queue, never push" in _reduce_context_switch_section(AgentSlices.resolve())

    set_fleet_configuration(reduce_context_switch=False)
    assert _reduce_context_switch_section(AgentSlices.resolve()) == ""


def test_prompt_section_reduce_context_switch_content(
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """Keep interruption limits resident and route procedures to the skill."""
    from ava_builtins.plugins.ava_fleet.agent_runtime import (
        _reduce_context_switch_section,
    )

    set_fleet_configuration(reduce_context_switch=True)
    section = _reduce_context_switch_section(AgentSlices.resolve())

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
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
):
    """End to end: the toggle gates the section's presence in the assembled
    system prompt."""

    section, slices = "## Reduce context switch for the human", AgentSlices.resolve()
    set_fleet_configuration(reduce_context_switch=True)
    assert section in build_system_prompt(fleet_registry(), slices, agent_id=1)

    set_fleet_configuration(reduce_context_switch=False)
    assert section not in build_system_prompt(fleet_registry(), slices, agent_id=1)


def test_fleet_operating_contract_is_loaded_on_demand(_load_activity_plugin: None):
    """The prompt routes chosen capabilities to complete, preserved procedures."""
    from ava_builtins.plugins.ava_fleet import agent_runtime

    section = agent_runtime._fleet_self_section(AgentSlices.resolve())
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


def test_task_conversion_absent_when_plugin_disabled():
    """Prompt copy and the task SDK reference disappear together with the
    fleet plugin."""
    prompt = build_system_prompt(EMPTY, AgentSlices.resolve(), agent_id=1)

    assert "## Fleet task interaction" not in prompt
    assert "create directly with `ava.tasks.create`" not in prompt
    assert "ava.tasks.create" not in prompt
    assert "One reporter per milestone" not in prompt


def test_spawn_label_param_gated_on_plugin(_load_activity_plugin: None):
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
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
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


def test_plugin_registers_ui_notice_members(_load_activity_plugin: None):
    # The whole notice surface is a push to the user's queue — registered on the
    # ui namespace (not self), gated on this plugin: it depends on a human
    # supervising.
    for name in ("notify", "edit_notice", "dismiss_notice"):
        assert callable(getattr(ava.ui, name))  # type: ignore[attr-defined]
        assert name in ava.ui.__all_for_ava__


def _notices(db_conn: psycopg.Connection, agent_id: int) -> list[tuple]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, content, priority, require_response, blocking, "
            "resolved_at, resolution, reply FROM agent_notices "
            "WHERE agent_id = %s ORDER BY local_id",
            (agent_id,),
        )
        return cur.fetchall()


def _assert_first_notice_is_sole_pending_fyi(nid: Any) -> None:
    assert nid.pending_count == 1
    assert len(nid.pending_notices) == 1
    assert nid.pending_notices[0]["id"] == nid
    assert nid.pending_notices[0]["title"] == "migration done"
    assert nid.pending_notices[0]["priority"] == "P1"
    assert "created_at" in nid.pending_notices[0]
    assert nid.superseded == []


def _assert_second_notice_supersedes_first(nid: Any, nid2: Any) -> None:
    assert nid2.pending_count == 1
    assert [n["title"] for n in nid2.pending_notices] == ["hit a rate limit"]
    assert nid2.superseded == [nid]


def test_notify_inserts_fyi_and_snapshot_counts_unread(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    # require_response defaults False -> these are FYI notices.
    nid = ava.ui.notify(
        "migration done", content="14k rows", priority="P1", idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    assert isinstance(nid, int)  # Notice is an int subclass — backward compatible
    _assert_first_notice_is_sole_pending_fyi(nid)

    # Posting a second notice auto-resolves the first (at most one).
    nid2 = ava.ui.notify("hit a rate limit", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    _assert_second_notice_supersedes_first(nid, nid2)

    db_conn.rollback()  # notify() committed via its own cursor; refresh our view
    rows = _notices(db_conn, agent_id)
    # First notice is now superseded; second is open.
    assert rows[0][0] == "migration done"
    assert rows[0][5] is not None  # resolved_at set
    assert rows[0][6] == "superseded"
    assert rows[1][0] == "hit a rate limit"
    assert rows[1][5] is None  # resolved_at (open)

    # the snapshot badge counts only the open FYI notice as unread.
    snap = select_one(db_conn, agent_id)
    assert snap is not None
    assert snap.unread_notice_count == 1
    assert snap.notices_awaiting_response == []


def test_notify_require_response_rides_awaiting_worklist(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify(  # type: ignore[attr-defined]
        "Send the release email?",
        content="A) yes\nB) no",
        priority="P0",
        require_response=True,
        blocking=True,
        idempotency_key=str(uuid4()),
    )
    # Posting a second require_response notice auto-resolves the first.
    nid2 = ava.ui.notify("Name the branch?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert nid2.superseded != []  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    db_conn.rollback()
    # Only the most recent require_response notice rides the snapshot worklist.
    snap = select_one(db_conn, agent_id)
    assert snap is not None
    assert snap.unread_notice_count == 0
    awaiting = snap.notices_awaiting_response
    assert [n.title for n in awaiting] == ["Name the branch?"]
    assert awaiting[0].content is None
    assert awaiting[0].blocking is False  # blocking defaults False


def _seed_task(db: psycopg.Connection, owner: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner) "
            "VALUES ('t', 'd', 'user', %s) RETURNING id",
            (owner,),
        )
        row = cur.fetchone()
    assert row is not None
    db.commit()
    return row[0]


def _notice_task_id(db: psycopg.Connection, agent_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT task_id FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_notify_records_task_id_and_rides_snapshot(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    """notify(task=...) writes the task link, and a require_response notice carries
    it out on the snapshot's notices_awaiting_response."""
    agent_id = _seed_agent(db_conn)
    tid = _seed_task(db_conn, agent_id)
    pin_agent(agent_id)
    ava.ui.notify(
        "stalled on a decision", require_response=True, task=tid, idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    db_conn.rollback()  # notify committed via its own cursor; refresh our view
    assert _notice_task_id(db_conn, agent_id) == tid
    snap = select_one(db_conn, agent_id)
    assert snap is not None
    assert [n.task_id for n in snap.notices_awaiting_response] == [tid]


def test_notify_without_task_leaves_task_id_null(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify("fyi, no task", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    db_conn.rollback()
    assert _notice_task_id(db_conn, agent_id) is None


def test_notify_nonexistent_task_raises(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="task 999999 does not exist"):
        ava.ui.notify("names a ghost task", task=999999, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]


def test_notify_validates_title_priority_and_blocking(_load_activity_plugin: None):
    with pytest.raises(ValueError, match="title"):
        ava.ui.notify("   ", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="priority"):
        ava.ui.notify("ok", priority="P9", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    # blocking is a strict subset of require_response: an FYI can never stall you.
    with pytest.raises(ValueError, match="require_response"):
        ava.ui.notify("ok", blocking=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]


def test_edit_notice_partial_update(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    nid = ava.ui.notify(
        "draft title", content="old body", priority="P2", idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    # change only title + priority; content is left as-is (omitted != cleared).
    ava.ui.edit_notice(title="new title", priority="P0")  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, content, priority, updated_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "new title"
    assert row[1] == "old body"  # untouched
    assert row[2] == "P0"
    assert row[3] is not None  # updated_at stamped

    # passing content=None explicitly clears it (the _UNSET sentinel
    # distinguishes "leave alone" from "erase").
    ava.ui.edit_notice(content=None)  # type: ignore[attr-defined]
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None and row[0] is None


def test_response_notice_content_edits_publish_refreshed_snapshot(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Content-only edits invalidate the selected agent's authoritative read.

    NoticePosted refreshes the Inbox queue; AgentUpdated requests current
    detail so the inspector can read the edited response-required notice.
    """
    from gateway.agents import notices as notices_router

    published_agent_ids: list[int] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        published_agent_ids.append(published_agent_id)

    # The route must publish this after every durable create/edit. It is absent
    # before the regression fix, so the assertion below proves the missing
    # inspector projection rather than merely the row's database state.
    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify(  # type: ignore[attr-defined]
        "decision needed",
        content="revision 0",
        priority="P1",
        require_response=True,
        blocking=True,
        idempotency_key=str(uuid4()),
    )
    for revision in range(1, 11):
        content = f"revision {revision}"
        ava.ui.edit_notice(content=content)  # type: ignore[attr-defined]

    # One AgentUpdated for creation plus one per content-only edit keeps the
    # inspector's cached snapshot authoritative through the whole chain.
    assert published_agent_ids == [agent_id] * 11
    db_conn.rollback()
    snapshot = select_one(db_conn, agent_id)
    assert snapshot is not None
    awaiting = snapshot.notices_awaiting_response
    assert len(awaiting) == 1
    notice = awaiting[0]
    assert notice.title == "decision needed"
    assert notice.content == "revision 10"
    assert notice.priority == "P1"
    assert notice.blocking is True


def test_edit_notice_validation_and_guards(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify("fyi notice", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]

    # nothing passed -> nothing to change.
    with pytest.raises(ValueError, match="at least one field"):
        ava.ui.edit_notice()  # type: ignore[attr-defined]
    # bad priority.
    with pytest.raises(ValueError, match="priority"):
        ava.ui.edit_notice(priority="P9")  # type: ignore[attr-defined]
    # blocking=True on an FYI (require_response False) is rejected.
    with pytest.raises(ValueError, match="needs a response"):
        ava.ui.edit_notice(blocking=True)  # type: ignore[attr-defined]
    # no open notice is idempotent (no-op).
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]
    ava.ui.edit_notice(title="too late")  # type: ignore[attr-defined]


def test_dismiss_notice_withdraws(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    nid = ava.ui.notify("stale fyi", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None  # resolved_at set
    assert row[1] == "withdrawn"

    # the dismissed notice drops off the unread badge.
    snap = select_one(db_conn, agent_id)
    assert snap is not None
    assert snap.unread_notice_count == 0

    # dismissing again is idempotent (no-op).
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]


def test_supersede_and_withdraw_publish_notice_resolved_for_both_kinds(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every agent-side resolution — supersede (a newer notice) or withdraw
    (dismiss_notice) — publishes notice_resolved regardless of require_response.
    The unified inbox refreshes its resolved history off this event alone, so a
    require_response notice resolved without one would leave the open list yet
    never surface in the resolved history."""
    # Events now publish from the gateway (R3 door ④ unified write API), not
    # the SDK — patch the gateway-side publisher.
    import ops.lifecycle as ops_mod

    resolved: list[int] = []

    async def _fake_publish(_bus: object, _aid: int, global_id: int) -> None:
        resolved.append(global_id)

    monkeypatch.setattr(ops_mod, "publish_notice_resolved", _fake_publish)

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify("Q1?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert resolved == []  # the first post resolves nothing
    # A second require_response notice supersedes the first — must publish even
    # though the superseded notice needed a response.
    ava.ui.notify("Q2?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert len(resolved) == 1
    # Withdrawing the surviving require_response notice publishes too.
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]
    assert len(resolved) == 2


def test_fleet_spawn_preserves_caller_creation_key(
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
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


def test_fleet_spawn_forwards_strong_mode_and_rejects_fork(
    _load_activity_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> int:
        captured.append(kwargs)
        return 42

    monkeypatch.setattr(ava.gateway_client, "spawn", capture)
    assert ava.agents.spawn(prompt="goal", idempotency_key="intent", require_idempotency=True) == 42
    assert captured[0]["require_idempotency"] is True
    assert "require_idempotency" in inspect.signature(ava.agents.spawn).parameters
    with pytest.raises(ValueError, match="does not support fork_from"):
        ava.agents.spawn(fork_from=1, idempotency_key="intent", require_idempotency=True)
    assert len(captured) == 1
