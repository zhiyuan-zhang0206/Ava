"""`gateway/routers/tasks.py` — PATCH /api/tasks/{id} validation.

The gateway task-update endpoint mirrors the SDK's always-on-reminder and
owned-task invariants at the HTTP boundary:

- remind_interval_seconds: an explicit null is rejected (reminders cannot be disabled);
  a value must be positive and <= 24h.
- owner: an explicit null is rejected (a task cannot be released). A non-null
  owner reassigns with a plain column write (no message to the affected agents).
- title: renames; a title colliding with another in_progress task's is
  rejected (mirrors the SDK duplicate-title invariant).
- parent close: status done/cancelled is rejected while a direct child remains
  in_progress.
- root task: the system root task (is_root=TRUE) is immutable — any PATCH
  targeting it is rejected with 422 before any write.
"""

from __future__ import annotations

import httpx2
import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _make_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (aid,),  # pyright: ignore[reportUnknownArgumentType]
        )
    db.commit()
    return aid  # pyright: ignore[reportUnknownVariableType]


def _make_task(
    db: psycopg.Connection,
    *,
    owner: int,
    remind_interval_seconds: int = 1800,
    title: str = "t",
    status: str = "in_progress",
    parent_id: int | None = None,
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks "
            "(title, description, status, owner, created_by, remind_interval_seconds, parent_id) "
            "VALUES (%s, 'd', %s, %s, 'user', %s, %s) RETURNING id",
            (title, status, owner, remind_interval_seconds, parent_id),
        )
        tid = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return tid  # pyright: ignore[reportUnknownVariableType]


def _make_root_task(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'ongoing', 'system', TRUE) RETURNING id"
        )
        tid = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return tid  # pyright: ignore[reportUnknownVariableType]


def _status(db: psycopg.Connection, tid: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT status FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _remind_interval_seconds(db: psycopg.Connection, tid: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT remind_interval_seconds FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _owner(db: psycopg.Connection, tid: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT owner FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _priority(db: psycopg.Connection, tid: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT priority FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


class TestRemindInterval:
    def test_null_is_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"remind_interval_seconds": None})
        assert resp.status_code == 422
        assert "cannot be disabled" in resp.json()["detail"]
        assert _remind_interval_seconds(db_conn, tid) == 1800  # unchanged

    def test_over_24h_is_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"remind_interval_seconds": 86401})
        assert resp.status_code == 422
        assert _remind_interval_seconds(db_conn, tid) == 1800

    def test_non_positive_is_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"remind_interval_seconds": 0})
        assert resp.status_code == 422
        assert _remind_interval_seconds(db_conn, tid) == 1800

    def test_valid_value_is_written(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"remind_interval_seconds": 86400})
        assert resp.status_code == 200
        assert resp.json()["remind_interval_seconds"] == 86400
        assert _remind_interval_seconds(db_conn, tid) == 86400


class TestOwner:
    def test_null_is_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"owner": None})
        assert resp.status_code == 422
        assert "cannot be released" in resp.json()["detail"]
        assert _owner(db_conn, tid) == owner  # unchanged

    def test_reassign_is_a_plain_write(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        new_owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"owner": new_owner})
        assert resp.status_code == 200
        assert resp.json()["owner"] == new_owner
        assert _owner(db_conn, tid) == new_owner


class TestPriority:
    def test_get_exposes_priority(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)  # defaults to P2
        with TestClient(app) as client:
            resp = client.get("/api/tasks")
        assert resp.status_code == 200
        row = next(t for t in resp.json()["tasks"] if t["id"] == tid)
        assert row["priority"] == "P2"

    def test_patch_sets_priority(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"priority": "P0"})
        assert resp.status_code == 200
        assert resp.json()["priority"] == "P0"
        assert _priority(db_conn, tid) == "P0"

    def test_patch_rejects_bad_priority(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"priority": "P9"})
        # Rejected by pydantic (the body types priority as the shared Priority
        # enum), so the 422 envelope carries its structured list in `errors`.
        assert resp.status_code == 422
        assert "P9" in str(resp.json()["errors"])
        assert _priority(db_conn, tid) == "P2"  # unchanged


class TestTitle:
    def test_patch_renames(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"title": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "renamed"

    def test_duplicate_in_progress_title_is_rejected(self, db_conn: psycopg.Connection) -> None:
        """Renaming to another in_progress task's title is rejected —
        mirrors the SDK create()/update() invariant."""
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner)  # holds title 't'
        tid = _make_task(db_conn, owner=owner, title="other")
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"title": "t"})
        assert resp.status_code == 422
        assert "already exists" in resp.json()["detail"]


class TestRootTaskImmutable:
    def test_status_change_is_rejected(self, db_conn: psycopg.Connection) -> None:
        root_id = _make_root_task(db_conn)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{root_id}", json={"status": "done"})
        assert resp.status_code == 422
        assert "root task" in resp.json()["detail"]
        assert _status(db_conn, root_id) == "ongoing"  # unchanged

    def test_reassign_is_rejected(self, db_conn: psycopg.Connection) -> None:
        root_id = _make_root_task(db_conn)
        new_owner = _make_agent(db_conn)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{root_id}", json={"owner": new_owner})
        assert resp.status_code == 422
        assert _owner(db_conn, root_id) is None  # still unowned

    def test_missing_task_still_404s(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.patch("/api/tasks/999999", json={"status": "done"})
        assert resp.status_code == 404

    def test_patch_ongoing_status_rejected(self, db_conn: psycopg.Connection) -> None:
        """'ongoing' is the system root's permanent state — a regular task
        cannot be patched into it."""
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, title="regular")
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"status": "ongoing"})
        assert resp.status_code == 422
        assert "permanent state" in resp.json()["detail"]
        assert _status(db_conn, tid) == "in_progress"  # unchanged

    def test_patch_open_status_rejected(self, db_conn: psycopg.Connection) -> None:
        """The 'open' status is gone (user ruling 2026-08-29): PATCHing it
        into a task 422s through the narrowed valid-status list — zero shim."""
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, title="regular-open")
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"status": "open"})
        assert resp.status_code == 422
        assert "Must be one of: in_progress, done, cancelled" in resp.json()["detail"]
        assert _status(db_conn, tid) == "in_progress"  # unchanged


class TestParentClose:
    def test_done_with_in_progress_child_is_rejected_and_unchanged(
        self, db_conn: psycopg.Connection
    ) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent-active-child")
        child = _make_task(
            db_conn,
            owner=owner,
            title="active-child",
            status="in_progress",
            parent_id=parent,
        )
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{parent}", json={"status": "done"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            f"task {parent} has 1 in_progress child tasks (e.g. #{child}) — "
            "close or cancel them first"
        )
        assert _status(db_conn, parent) == "in_progress"

    def test_cancelled_with_in_progress_child_is_rejected(
        self, db_conn: psycopg.Connection
    ) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent-active-child")
        child = _make_task(
            db_conn,
            owner=owner,
            title="active-child",
            status="in_progress",
            parent_id=parent,
        )
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{parent}", json={"status": "cancelled"})
        assert resp.status_code == 422
        assert f"#{child}" in resp.json()["detail"]
        assert _status(db_conn, parent) == "in_progress"

    def test_all_children_closed_allows_parent_close(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent-closed-children")
        _make_task(
            db_conn,
            owner=owner,
            title="done-child",
            status="done",
            parent_id=parent,
        )
        _make_task(
            db_conn,
            owner=owner,
            title="cancelled-child",
            status="cancelled",
            parent_id=parent,
        )
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{parent}", json={"status": "done"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"


def _parent(db: psycopg.Connection, tid: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


class TestParent:
    def test_patch_reparents(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent")
        tid = _make_task(db_conn, owner=owner, title="child")
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"parent_id": parent})
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == parent

    def test_patch_null_moves_to_root(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent2")
        tid = _make_task(db_conn, owner=owner, title="child2")
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"parent_id": parent})
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == parent
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"parent_id": None})
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == root

    def test_patch_missing_parent_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"parent_id": 999_999})
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    def test_patch_self_parent_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(f"/api/tasks/{tid}", json={"parent_id": tid})
        assert resp.status_code == 422
        assert "own parent" in resp.json()["detail"]

    def test_patch_cycle_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        a = _make_task(db_conn, owner=owner, title="cycle-a")
        b = _make_task(db_conn, owner=owner, title="cycle-b")
        c = _make_task(db_conn, owner=owner, title="cycle-c")
        with TestClient(app) as client:
            assert client.patch(f"/api/tasks/{b}", json={"parent_id": a}).status_code == 200
            assert client.patch(f"/api/tasks/{c}", json={"parent_id": b}).status_code == 200
            resp = client.patch(f"/api/tasks/{a}", json={"parent_id": c})
        assert resp.status_code == 422
        assert "descendant" in resp.json()["detail"]


class TestTimestampOffset:
    """tz audit PR-1 behavior lock. `_row_to_task` builds `created_at`/
    `updated_at` with a bare `.isoformat()` on the value psycopg3 read back —
    the offset it carries is whatever the PG SESSION timezone was, not a
    fixed one. Pinning that session timezone to UTC (shared/pg_tools.py:
    pg_tz_args, cli/commands/_cluster_instance.py) is what makes this `+00:00`
    instead of drifting with the host OS timezone."""

    def test_get_created_at_has_utc_offset(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.get("/api/tasks")
        assert resp.status_code == 200
        row = next(t for t in resp.json()["tasks"] if t["id"] == tid)
        assert row["created_at"].endswith("+00:00")
        assert row["updated_at"].endswith("+00:00")


class TestGetTasksWindow:
    """GET /api/tasks?window= — backend-side last-activity filtering for the
    task graph time filter (Task #1969): window on updated_at, in_progress
    exemption, ghost ancestors for tree connectivity, done/cancelled outside
    the window omitted."""

    def _backdate(self, db: psycopg.Connection, tid: int, days: int) -> None:
        with db.cursor() as cur:
            # The placeholder stays outside the interval literal — a %s inside
            # quotes is mis-bound by psycopg3's client-side substitution.
            cur.execute(
                "UPDATE agent_tasks SET updated_at = now() - (%s * interval '1 day') WHERE id = %s",
                (days, tid),  # pyright: ignore[reportUnknownArgumentType]
            )
        db.commit()

    def _backdate_hours(self, db: psycopg.Connection, tid: int, hours: int) -> None:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET updated_at = now() - (%s * interval '1 hour') WHERE id = %s",
                (hours, tid),  # pyright: ignore[reportUnknownArgumentType]
            )
        db.commit()

    def _ids(self, resp: httpx2.Response) -> set[int]:
        return {t["id"] for t in resp.json()["tasks"]}

    def test_window_keeps_recent_and_in_progress_drops_old_done(
        self, db_conn: psycopg.Connection
    ) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        old_ip = _make_task(
            db_conn, owner=owner, status="in_progress", parent_id=root, title="old-ip"
        )
        recent_done = _make_task(
            db_conn, owner=owner, status="done", parent_id=root, title="recent-done"
        )
        old_done = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="old-done")
        self._backdate(db_conn, old_ip, 40)
        self._backdate(db_conn, old_done, 40)
        with TestClient(app) as client:
            resp = client.get("/api/tasks", params={"window": "7d"})
        assert resp.status_code == 200
        rows = {t["id"]: t for t in resp.json()["tasks"]}
        # Root + the old in_progress task are exempt; the recent done task is
        # inside the window; the old done task is dropped entirely.
        assert self._ids(resp) == {root, old_ip, recent_done}
        assert all(rows[tid]["ghost"] is False for tid in rows)

    def test_window_delivers_out_of_window_ancestor_as_ghost(
        self, db_conn: psycopg.Connection
    ) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        old_done_parent = _make_task(
            db_conn, owner=owner, status="done", parent_id=root, title="old-parent"
        )
        child = _make_task(
            db_conn, owner=owner, status="in_progress", parent_id=old_done_parent, title="child"
        )
        self._backdate(db_conn, old_done_parent, 40)
        with TestClient(app) as client:
            resp = client.get("/api/tasks", params={"window": "7d"})
        assert resp.status_code == 200
        rows = {t["id"]: t for t in resp.json()["tasks"]}
        assert self._ids(resp) == {root, old_done_parent, child}
        assert rows[child]["ghost"] is False
        # The out-of-window done ancestor is delivered for tree connectivity,
        # flagged ghost so the graph renders it dimmed.
        assert rows[old_done_parent]["ghost"] is True

    def test_24h_window_keeps_recent_done_and_drops_older(
        self, db_conn: psycopg.Connection
    ) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        recent = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="recent-20h")
        older = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="older-40h")
        self._backdate_hours(db_conn, recent, 20)
        self._backdate_hours(db_conn, older, 40)
        with TestClient(app) as client:
            resp = client.get("/api/tasks", params={"window": "24h"})
        assert resp.status_code == 200
        assert self._ids(resp) == {root, recent}
        assert older not in self._ids(resp)

    def test_window_delivers_a_chain_of_out_of_window_ancestors_as_ghosts(
        self, db_conn: psycopg.Connection
    ) -> None:
        # A deep subtree: done grandparent → cancelled parent → in_progress
        # child. Both ancestors sit outside the window and must arrive as
        # ghosts so the child never dangles.
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        gp = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="old-gp")
        parent = _make_task(db_conn, owner=owner, status="cancelled", parent_id=gp, title="old-p")
        child = _make_task(
            db_conn, owner=owner, status="in_progress", parent_id=parent, title="child"
        )
        self._backdate(db_conn, gp, 60)
        self._backdate(db_conn, parent, 50)
        with TestClient(app) as client:
            resp = client.get("/api/tasks", params={"window": "7d"})
        assert resp.status_code == 200
        rows = {t["id"]: t for t in resp.json()["tasks"]}
        assert self._ids(resp) == {root, gp, parent, child}
        assert rows[child]["ghost"] is False
        assert rows[gp]["ghost"] is True
        assert rows[parent]["ghost"] is True

    def test_30d_window_includes_older_activity(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        mid = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="mid-done")
        self._backdate(db_conn, mid, 20)
        with TestClient(app) as client:
            resp7 = client.get("/api/tasks", params={"window": "7d"})
            resp30 = client.get("/api/tasks", params={"window": "30d"})
        assert resp7.status_code == 200 and resp30.status_code == 200
        assert mid not in self._ids(resp7)
        assert mid in self._ids(resp30)

    def test_default_and_all_return_everything_unflagged(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        old_done = _make_task(db_conn, owner=owner, status="done", parent_id=root, title="old-done")
        self._backdate(db_conn, old_done, 400)
        with TestClient(app) as client:
            plain = client.get("/api/tasks")
            allp = client.get("/api/tasks", params={"window": "all"})
        assert plain.status_code == 200 and allp.status_code == 200
        for resp in (plain, allp):
            rows = resp.json()["tasks"]
            assert old_done in self._ids(resp)
            assert all(t["ghost"] is False for t in rows)

    def test_unknown_window_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.get("/api/tasks", params={"window": "99d"})
        assert resp.status_code == 422
