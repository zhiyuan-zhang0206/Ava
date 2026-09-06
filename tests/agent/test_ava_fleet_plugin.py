"""ava_builtins.plugins.ava_fleet integration.

Two layers:
- plugin load end-to-end (`_load_extensions` real path): registers the
  `ava.self.set_label` / `get_label` self members + the `ava.ui.notify` /
  `edit_notice` / `dismiss_notice` push members + a system-prompt section.
- `set_label()` / `get_label()` write/read the agent's own label (sticky, so the
  labeler won't overwrite it) — reflected in the agent snapshot (the single
  source the monitoring view reads). `notify()` / `edit_notice()` /
  `dismiss_notice()` write and read the unified agent_notices queue
  (migration 0053). At most one notice is open per agent (notify auto-resolves
  the previous one), so edit/dismiss take no id.
"""

import importlib
import inspect
import sys
from collections.abc import Iterator

import psycopg
import pytest

import ava
import ava.agents
from agent.graph._system_prompt import build_system_prompt
from agent.state import clear_plugin_registrations
from shared.agent_snapshot import select_one
from shared.plugin_context import PluginContext


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

    from gateway.app import app

    with TestClient(app, base_url="http://test-gateway") as tc:
        monkeypatch.setattr("ava._gateway_transport._client", tc)
        yield


@pytest.fixture
def _load_activity_plugin() -> Iterator[None]:
    """Load plugins.ava_fleet via the real PluginContext path; tear down the
    member + prompt-section + module cache so tests do not leak into each other
    (a second import would hit MemberConflictError)."""
    clear_plugin_registrations()
    ava.clear_registered_namespaces()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_fleet"):
            del sys.modules[name]

    with PluginContext("ava_fleet"):
        from ava_builtins.plugins.ava_fleet import plugin as plugin

    yield

    # clear_plugin_registrations() runs ava._extend.clear_wraps(), which restores
    # ava.agents.spawn to the captured core original — no reload needed.
    clear_plugin_registrations()
    ava.clear_registered_namespaces()


def test_plugin_registers_self_members(_load_activity_plugin: None):
    for name in ("set_label", "get_label"):
        assert callable(getattr(ava.self, name))
        assert name in ava.self.__all_for_ava__


def test_member_torn_down_on_clear(_load_activity_plugin: None):
    # The fixture loaded the plugin; clearing must remove the member from both
    # the module and its __all_for_ava__ so a reload re-registers from empty state.
    ava.clear_registered_namespaces()
    assert not hasattr(ava.self, "set_label")
    assert "set_label" not in ava.self.__all_for_ava__


def test_plugin_registers_prompt_section(_load_activity_plugin: None):
    prompt = build_system_prompt()
    assert "ava.self.set_label" in prompt


def test_prompt_assigns_shared_milestone_reporting(_load_activity_plugin: None):
    """The rendered prompt carries the reporting contract with the plugin."""
    prompt = build_system_prompt()

    assert prompt.count("One reporter per milestone") == 1
    assert "single reporter and action owner" in prompt
    assert "do not ask another agent to relay the same result" in prompt
    assert "new evidence, a blocker, or a changed result" in prompt
    assert "that write can notify the task owner too" in prompt


def test_prompt_section_idle_vs_terminate_rule(_load_activity_plugin: None):
    """The Long-running agent section must make the end-of-turn choice
    explicit in plain language: waiting on a known event -> idle; all done ->
    end your own process (no idle standing by); unsure whether more follows ->
    still end your own process (being brought back is cheaper than standing
    by). The delegator side says a finished worker ends itself. SDK call
    names stay out of the fleet section — the agent decides the mechanics
    (heartbeat pause etc.) itself."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()
    # Waiting branch: end the turn idle, the awaited event still wakes you.
    assert "end the turn idle" in section
    # Done branch: end your own process instead of idling on.
    assert "end your own process" in section
    # Unsure branch: being brought back later is cheaper than standing by.
    assert "cheaper than standing by" in section
    # Delegator side: a finished worker ends itself; ending it is the fallback.
    assert "do not plan to terminate it yourself" in section
    # Semantic-only: no SDK call names in the fleet section; the mechanics
    # (pause_heartbeat etc.) are the agent's own call.
    assert "pause_heartbeat" not in section
    assert "ava.self.terminate" not in section
    assert "ava.agents.terminate" not in section


def test_prompt_section_dismiss_notice_after_dialog_reply(
    _load_activity_plugin: None,
):
    """When the user has already answered in the dialog, the agent must
    actively dismiss the pending notice instead of leaving it open — and the
    rule is phrased semantically (no dismiss_notice call name)."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()
    assert "dismiss that notice yourself" in section
    assert "already replied in the dialog" in section
    assert "dismiss_notice" not in section


def test_prompt_section_queue_delivery_mandate(
    _load_activity_plugin: None,
):
    """The Fleet section must make queue delivery mandatory: what the user
    must decide (or should know) is delivered through the queue — never left
    in chat for the user to discover later — and it is queued even when the
    user cannot be reached (offline, or not in this dialog); pending decision
    points merge into one numbered notice so a single reply settles them;
    posting is delivery, no staging for a later moment. Phrased semantically
    and self-contained — no skill names, no call names beyond the channel the
    section already names."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()
    assert "Queue delivery is mandatory" in section
    assert "never left in the chat" in section
    assert "offline" in section
    assert "numbered notice" in section
    assert "Posting IS delivery" in section
    assert "reduce-context-switch" not in section


def test_prompt_section_task_conversion_contract(_load_activity_plugin: None):
    """The fleet section turns a future signal into an owned, deduplicated
    task without inventing registry routing behavior."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()
    assert "## Fleet task interaction" in section
    assert "create directly with `ava.tasks.create`" in section
    assert "do not add an ask-someone-first round" in section
    assert "parent's active children" in section
    assert "one business delivery to the current delegator" in section
    assert "`created_by` is an audit trail, not a routing field" in section
    assert "no automatic notification to the creator" in section
    assert "reserve `ava.tasks.create_and_assign` for when the owner must be spawned" in section


def test_prompt_section_task_conversion_is_domain_instance_only(
    _load_activity_plugin: None,
):
    """The fleet section names task mechanics without repeating the framework's
    cross-domain future-signal rule or platform-specific policy."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()
    for phrase in (
        "when in doubt, record it",
        "act on it this turn",
        "Choose the smallest action",
        "costs every later agent",
    ):
        assert phrase not in section
    assert not any(character.isdigit() for character in section)
    assert "CI" not in section
    assert "flake" not in section


def test_prompt_section_numeric_identifier_prefixes(_load_activity_plugin: None):
    """Fleet references identify agents, tasks, and pull requests by kind."""
    from ava_builtins.plugins.ava_fleet.plugin import _fleet_self_section

    section = _fleet_self_section()

    for identifier in ("Ava #<id>", "task #<id>", "PR #<id>"):
        assert identifier in section
    assert "A bare number is ambiguous" in section


def test_task_conversion_absent_when_plugin_disabled():
    """Prompt copy and the task SDK reference disappear together with the
    fleet plugin."""
    clear_plugin_registrations()
    ava.clear_registered_namespaces()

    prompt = build_system_prompt()

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


def test_set_and_get_label_sticky(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        assert ava.self.get_label() == ""  # type: ignore[attr-defined]

        ava.self.set_label("auth-refactor lead")  # type: ignore[attr-defined]
        assert ava.self.get_label() == "auth-refactor lead"  # type: ignore[attr-defined]
        snap = select_one(db_conn, agent_id)
        assert snap is not None and snap.label == "auth-refactor lead"

        # Self-set flips the sticky bit so the labeler's CAS won't overwrite.
        with db_conn.cursor() as cur:
            cur.execute("SELECT label_user_set FROM agents WHERE id=%s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] is True

        # Empty string clears back to the default (#N fallback).
        ava.self.set_label("")  # type: ignore[attr-defined]
        assert ava.self.get_label() == ""  # type: ignore[attr-defined]
    finally:
        ava._boot._agent_id = original


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


def test_notify_inserts_fyi_and_snapshot_counts_unread(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        # require_response defaults False -> these are FYI notices.
        nid = ava.ui.notify("migration done", content="14k rows", priority="P1")  # type: ignore[attr-defined]
        assert isinstance(nid, int)  # Notice is an int subclass — backward compatible
        assert nid.pending_count == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert len(nid.pending_notices) == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType, reportUnknownMemberType]
        assert nid.pending_notices[0]["id"] == nid  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert nid.pending_notices[0]["title"] == "migration done"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert nid.pending_notices[0]["priority"] == "P1"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert "created_at" in nid.pending_notices[0]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert nid.superseded == []  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

        # Posting a second notice auto-resolves the first (at most one).
        nid2 = ava.ui.notify("hit a rate limit")  # type: ignore[attr-defined]
        assert nid2.pending_count == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert [n["title"] for n in nid2.pending_notices] == ["hit a rate limit"]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert nid2.superseded == [nid]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

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
    finally:
        ava._boot._agent_id = original


def test_notify_require_response_rides_awaiting_worklist(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify(  # type: ignore[attr-defined]
            "Send the release email?",
            content="A) yes\nB) no",
            priority="P0",
            require_response=True,
            blocking=True,
        )
        # Posting a second require_response notice auto-resolves the first.
        nid2 = ava.ui.notify("Name the branch?", require_response=True)  # type: ignore[attr-defined]
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
    finally:
        ava._boot._agent_id = original


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
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("stalled on a decision", require_response=True, task=tid)  # type: ignore[attr-defined]
        db_conn.rollback()  # notify committed via its own cursor; refresh our view
        assert _notice_task_id(db_conn, agent_id) == tid
        snap = select_one(db_conn, agent_id)
        assert snap is not None
        assert [n.task_id for n in snap.notices_awaiting_response] == [tid]
    finally:
        ava._boot._agent_id = original


def test_notify_without_task_leaves_task_id_null(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("fyi, no task")  # type: ignore[attr-defined]
        db_conn.rollback()
        assert _notice_task_id(db_conn, agent_id) is None
    finally:
        ava._boot._agent_id = original


def test_notify_nonexistent_task_raises(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="task 999999 does not exist"):
            ava.ui.notify("names a ghost task", task=999999)  # type: ignore[attr-defined]
    finally:
        ava._boot._agent_id = original


def test_notify_validates_title_priority_and_blocking(_load_activity_plugin: None):
    with pytest.raises(ValueError, match="title"):
        ava.ui.notify("   ")  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="priority"):
        ava.ui.notify("ok", priority="P9")  # type: ignore[attr-defined]
    # blocking is a strict subset of require_response: an FYI can never stall you.
    with pytest.raises(ValueError, match="require_response"):
        ava.ui.notify("ok", blocking=True)  # type: ignore[attr-defined]


def test_edit_notice_partial_update(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        nid = ava.ui.notify("draft title", content="old body", priority="P2")  # type: ignore[attr-defined]
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
    finally:
        ava._boot._agent_id = original


def test_response_notice_content_edits_publish_refreshed_snapshot(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Content-only edits keep a response-required notice in the live snapshot.

    The inspector consumes ``AgentUpdated.snapshot.notices_awaiting_response``.
    ``NoticePosted`` refreshes the unified Inbox queue, while this snapshot
    refreshes the inspector's response-required worklist and its detail body.
    """
    from gateway.routers import notices as notices_router

    published_agent_ids: list[int] = []

    def _capture_snapshot(_conn: psycopg.Connection, published_agent_id: int) -> None:
        published_agent_ids.append(published_agent_id)

    # The route must publish this after every durable create/edit. It is absent
    # before the regression fix, so the assertion below proves the missing
    # inspector projection rather than merely the row's database state.
    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify(  # type: ignore[attr-defined]
            "decision needed",
            content="revision 0",
            priority="P1",
            require_response=True,
            blocking=True,
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
    finally:
        ava._boot._agent_id = original


def test_edit_notice_validation_and_guards(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("fyi notice")  # type: ignore[attr-defined]

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
    finally:
        ava._boot._agent_id = original


def test_dismiss_notice_withdraws(_load_activity_plugin: None, db_conn: psycopg.Connection):
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        nid = ava.ui.notify("stale fyi")  # type: ignore[attr-defined]
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
    finally:
        ava._boot._agent_id = original


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
    import ops.ops_lifecycle as ops_mod

    resolved: list[int] = []

    async def _fake_publish(_aid: int, global_id: int) -> None:
        resolved.append(global_id)

    monkeypatch.setattr(ops_mod, "publish_notice_resolved", _fake_publish)

    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("Q1?", require_response=True)  # type: ignore[attr-defined]
        assert resolved == []  # the first post resolves nothing
        # A second require_response notice supersedes the first — must publish even
        # though the superseded notice needed a response.
        ava.ui.notify("Q2?", require_response=True)  # type: ignore[attr-defined]
        assert len(resolved) == 1
        # Withdrawing the surviving require_response notice publishes too.
        ava.ui.dismiss_notice()  # type: ignore[attr-defined]
        assert len(resolved) == 2
    finally:
        ava._boot._agent_id = original


def test_dismissing_response_notice_refreshes_inspector_snapshot(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Removing the dismiss snapshot refresh leaves the inspector stale."""
    from gateway.routers import notices as notices_router

    published_awaiting: list[list[str]] = []

    def _capture_snapshot(conn: psycopg.Connection, published_agent_id: int) -> None:
        snapshot = select_one(conn, published_agent_id)
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("decision needed", require_response=True)  # type: ignore[attr-defined]
        ava.ui.dismiss_notice()  # type: ignore[attr-defined]

        # The first snapshot announces the newly posted question. Dismissal
        # must publish a second, now-empty snapshot for the inspector.
        assert published_awaiting == [["decision needed"], []]
    finally:
        ava._boot._agent_id = original


def test_cross_type_supersede_refreshes_inbox_and_inspector_projections(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Each cross-type replacement announces both consumers' new state."""
    from gateway.routers import notices as notices_router

    published_awaiting: list[list[str]] = []
    posted: list[int] = []
    resolved: list[int] = []

    def _capture_snapshot(conn: psycopg.Connection, published_agent_id: int) -> None:
        snapshot = select_one(conn, published_agent_id)
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    async def _capture_posted(_agent_id: int, notice_id: int, *_args: object) -> None:
        posted.append(notice_id)

    async def _capture_resolved(_agent_id: int, notice_id: int) -> None:
        resolved.append(notice_id)

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )
    monkeypatch.setattr(notices_router._ops, "publish_notice_posted", _capture_posted)
    monkeypatch.setattr(notices_router._ops, "publish_notice_resolved", _capture_resolved)

    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        ava.ui.notify("FYI old")  # type: ignore[attr-defined]
        ava.ui.notify("question", require_response=True)  # type: ignore[attr-defined]
        ava.ui.notify("FYI new")  # type: ignore[attr-defined]

        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, title FROM agent_notices WHERE agent_id = %s ORDER BY local_id",
                (agent_id,),
            )
            notice_ids = {str(title): int(notice_id) for notice_id, title in cur.fetchall()}

        # NoticeResolved evicts each old Inbox row; NoticePosted adds each new
        # one. AgentUpdated shows the question appear, then disappear when its
        # FYI replacement supersedes it.
        assert resolved == [notice_ids["FYI old"], notice_ids["question"]]
        assert posted == [
            notice_ids["FYI old"],
            notice_ids["question"],
            notice_ids["FYI new"],
        ]
        assert published_awaiting == [["question"], []]
    finally:
        ava._boot._agent_id = original


def test_notice_return_int_and_edit_dismiss_take_no_id(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    """notify returns a Notice (int subclass); edit/dismiss act on the single
    open notice with no id argument."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        notice = ava.ui.notify("hold this", content="body", priority="P2")  # type: ignore[attr-defined]
        assert isinstance(notice, int)
        assert int(notice) == notice  # int conversion gives the id

        # edit acts on the open notice — no id passed.
        ava.ui.edit_notice(title="updated title")  # type: ignore[attr-defined]
        # dismiss acts on the open notice — no id passed.
        ava.ui.dismiss_notice()  # type: ignore[attr-defined]

        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT title, resolved_at, resolution FROM agent_notices WHERE agent_id = %s AND local_id = %s",
                (agent_id, notice),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "updated title"
        assert row[1] is not None
        assert row[2] == "withdrawn"

        # After dismissal, new notify should show pending_count = 1 (the fresh one)
        nid2 = ava.ui.notify("fresh fyi")  # type: ignore[attr-defined]
        assert nid2.pending_count == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert nid2.pending_notices[0]["id"] == nid2  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    finally:
        ava._boot._agent_id = original
