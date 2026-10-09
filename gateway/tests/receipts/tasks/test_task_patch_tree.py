"""Keyed task PATCH preserves parent/child and closed-tree invariants."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers.tests.test_tasks_router import (
    _make_agent,
    _make_root_task,
    _make_task,
    _status,
)


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
            resp = client.patch(
                f"/api/tasks/{parent}",
                json={"status": "done"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            f"task {parent} has 1 in_progress child tasks (e.g. #{child}) — close or cancel them first"
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
            resp = client.patch(
                f"/api/tasks/{parent}",
                json={"status": "cancelled"},
                headers={"Idempotency-Key": str(uuid4())},
            )
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
            resp = client.patch(
                f"/api/tasks/{parent}",
                json={"status": "done"},
                headers={"Idempotency-Key": str(uuid4())},
            )
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
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": parent},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == parent

    def test_patch_null_moves_to_root(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        root = _make_root_task(db_conn)
        parent = _make_task(db_conn, owner=owner, title="parent2")
        tid = _make_task(db_conn, owner=owner, title="child2")
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": parent},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == parent
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": None},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 200
        assert _parent(db_conn, tid) == root

    @pytest.mark.parametrize("closed_status", ["done", "cancelled"])
    def test_patch_closed_parent_rejected(
        self, db_conn: psycopg.Connection, closed_status: str
    ) -> None:
        """PATCH mirrors the SDK reparent check: moving a task under a closed
        (done / cancelled) parent is a 422 — a closed task never gains
        children (task #1975)."""
        owner = _make_agent(db_conn)
        parent = _make_task(
            db_conn,
            owner=owner,
            title=f"closed-parent-{closed_status}",
            status=closed_status,
        )
        tid = _make_task(db_conn, owner=owner, title=f"child-{closed_status}")
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": parent},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "closed parent" in resp.json()["detail"]
        # The tree is unchanged: the child has no parent.
        assert _parent(db_conn, tid) is None

    def test_patch_missing_parent_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": 999_999},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    def test_patch_self_parent_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner)
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/tasks/{tid}",
                json={"parent_id": tid},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "own parent" in resp.json()["detail"]

    def test_patch_cycle_rejected(self, db_conn: psycopg.Connection) -> None:
        owner = _make_agent(db_conn)
        a = _make_task(db_conn, owner=owner, title="cycle-a")
        b = _make_task(db_conn, owner=owner, title="cycle-b")
        c = _make_task(db_conn, owner=owner, title="cycle-c")
        with TestClient(app) as client:
            assert (
                client.patch(
                    f"/api/tasks/{b}",
                    json={"parent_id": a},
                    headers={"Idempotency-Key": str(uuid4())},
                ).status_code
                == 200
            )
            assert (
                client.patch(
                    f"/api/tasks/{c}",
                    json={"parent_id": b},
                    headers={"Idempotency-Key": str(uuid4())},
                ).status_code
                == 200
            )
            resp = client.patch(
                f"/api/tasks/{a}", json={"parent_id": c}, headers={"Idempotency-Key": str(uuid4())}
            )
        assert resp.status_code == 422
        assert "descendant" in resp.json()["detail"]
