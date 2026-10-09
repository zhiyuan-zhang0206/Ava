"""Task registry cases: create unique title no conflict."""

from __future__ import annotations

import re
import threading
import time
from uuid import uuid4

import psycopg
import pytest

import ava
from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import (
    _ignore_system_note,
    _parent_of,
    _persisted_parent,
    _seed_agent,
)
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import (
    root_task_id as root_task_id,
)
from tests.fixtures.pin_agent import pin_agent


def test_create_unique_title_no_conflict(db_conn, root_task_id: int):
    """Creating a task with a truly unique title succeeds."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task1 = task_registry.create(
        "title a", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    task2 = task_registry.create(
        "title b", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    assert task1.id != task2.id


def test_update_note_with_cancel_appends_to_results(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note with status='cancelled' appends a timestamped line to results."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.log(task.id, "work started", operation_key=str(uuid4()))
    task_registry.update(
        task.id, status="cancelled", note="no longer needed", operation_key=str(uuid4())
    )
    results = task_registry.get(task.id).results
    assert results is not None
    lines = results.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("work started")
    assert lines[1].endswith("no longer needed")
    assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", lines[1])


def test_update_note_with_done_appends_to_results(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note works with any status, not just cancelled."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(task.id, status="done", note="all tests pass", operation_key=str(uuid4()))
    results = task_registry.get(task.id).results
    assert results is not None
    assert "all tests pass" in results


def test_update_note_standalone_no_status_change(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note alone (no status change) works as a drop-in for log()."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(task.id, note="progress update", operation_key=str(uuid4()))
    results = task_registry.get(task.id).results
    assert results is not None
    assert "progress update" in results
    # Status unchanged
    assert task_registry.get(task.id).status == "in_progress"


def test_update_note_with_no_prior_results(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When a task has no prior results, the note line is the only content."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    assert task.results is None
    task_registry.update(task.id, status="cancelled", note="duplicate", operation_key=str(uuid4()))
    results = task_registry.get(task.id).results
    assert results is not None
    assert "duplicate" in results


def test_update_note_with_results_overwrite_appends_after(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When results is also set, the note is appended after the new results."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(
        task.id,
        status="cancelled",
        results="final notes",
        note="out of scope",
        operation_key=str(uuid4()),
    )
    results = task_registry.get(task.id).results
    assert results is not None
    lines = results.splitlines()
    assert lines[0] == "final notes"
    assert lines[1].endswith("out of scope")


def test_update_note_none_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """note=None does nothing — no line appended."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(task.id, status="cancelled", note=None, operation_key=str(uuid4()))
    results = task_registry.get(task.id).results
    # None because no note was appended and no prior results existed
    assert results is None


def test_log_delegates_to_update_note(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """log() is a thin wrapper around update(note=...)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.log(task.id, "via log()", operation_key=str(uuid4()))
    results = task_registry.get(task.id).results
    assert results is not None
    assert "via log()" in results
    # Status unchanged
    assert task_registry.get(task.id).status == "in_progress"


def test_update_root_task_is_rejected(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """The system root task is immutable: any update() targeting it fails fast
    and leaves the row untouched."""
    agent_id = _seed_agent(db_conn)
    root_id = root_task_id
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="root task"):
        task_registry.update(root_id, status="done", operation_key=str(uuid4()))
    with pytest.raises(ValueError, match="root task"):
        task_registry.update(root_id, owner=agent_id, operation_key=str(uuid4()))
    # The rejected writes never landed — the root row is unchanged.
    root = task_registry.get(root_id)
    assert root.status == "in_progress"
    assert root.owner is None


@pytest.mark.parametrize("next_status", ["in_progress", "done", "cancelled"])
def test_update_statuses_are_peer_open(
    db_conn: psycopg.Connection,
    root_task_id: int,
    next_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any peer may write in_progress/done/cancelled — the 'ongoing' ownership
    gate was removed with the status itself (user ruling 2026-09-15), so no
    ownership rule remains on the status path."""
    owner = _seed_agent(db_conn)
    stranger = _seed_agent(db_conn)
    pin_agent(owner)
    task = task_registry.create(
        "peer-status-change", "detail", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    pin_agent(stranger)
    monkeypatch.setattr(ava.agents, "send_system_note", _ignore_system_note)

    task_registry.update(task.id, status=next_status, operation_key=str(uuid4()))

    assert task_registry.get(task.id).status == next_status


def test_zero_shim_update_status_open_is_rejected(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """The 'open' status is gone entirely (user ruling 2026-08-29): update()
    refuses it with the narrowed enum's error — no backward-compat shim."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "shim-check", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    with pytest.raises(ValueError, match="status must be one of"):
        task_registry.update(task.id, status="open", operation_key=str(uuid4()))
    assert task_registry.get(task.id).status == "in_progress"


def test_zero_shim_update_status_ongoing_is_rejected(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """The 'ongoing' status is gone entirely (user ruling 2026-09-15): update()
    refuses it with the three-value enum's error — no backward-compat shim."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "shim-check-ongoing", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    with pytest.raises(ValueError, match="status must be one of"):
        task_registry.update(task.id, status="ongoing", operation_key=str(uuid4()))
    assert task_registry.get(task.id).status == "in_progress"


@pytest.mark.parametrize("removed_status", ["open", "ongoing"])
def test_zero_shim_list_status_is_rejected(
    db_conn: psycopg.Connection, root_task_id: int, removed_status: str
) -> None:
    """list() refuses a removed status too — the read filter is the same enum
    ('open' went 2026-08-29, 'ongoing' went 2026-09-15)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task_registry.create(
        "shim-check-list", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    with pytest.raises(ValueError, match="status must be one of"):
        task_registry.list(status=removed_status)


def test_create_requires_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """`parent` is a required keyword-only argument — calling create() without
    it fails at the signature, so no task can silently land on the root."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(TypeError, match="missing 1 required keyword-only argument: 'parent'"):
        task_registry.create("no-parent", "detail", operation_key=str(uuid4()))  # pyright: ignore[reportCallIssue]


def test_create_with_root_parent_anchors_to_root(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A top-level task passes the system root task's id as its parent. The
    root itself is never made its own parent (it stays parent-less)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "anchored-to-root", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    assert task.parent_id == root_task_id
    # The persisted row agrees, and the root did not gain a parent.
    assert _parent_of(db_conn, task.id) == root_task_id
    assert _parent_of(db_conn, root_task_id) is None


def test_create_with_explicit_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A subtask passes the id of an existing task as its parent; the parent
    itself is top-level (parented by the system root)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        "explicit-parent", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    child = task_registry.create(
        "explicit-child", "detail", parent=parent.id, operation_key=str(uuid4())
    )
    assert parent.parent_id == root_task_id  # top-level task under the root
    assert child.parent_id == parent.id  # subtask under its parent


def test_create_rejects_missing_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A parent id that names no existing task is rejected with a friendly
    ValueError instead of a raw foreign-key violation."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="parent task 999999 does not exist"):
        task_registry.create("orphan", "detail", parent=999_999, operation_key=str(uuid4()))


@pytest.mark.parametrize("closed_status", ["done", "cancelled"])
def test_create_rejects_closed_parent(
    db_conn: psycopg.Connection, closed_status: str, root_task_id: int
) -> None:
    """A closed (done / cancelled) task must never gain children — creating a
    subtask under it is rejected instead of silently building the parent chain
    that produced false-orphan rows in the task graph (task #1975)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        f"closed-parent-{closed_status}", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    task_registry.update(parent.id, status=closed_status, operation_key=str(uuid4()))
    message = (
        f"parent task {parent.id} is {closed_status} — a closed task cannot be the "
        "parent of a new task"
    )
    with pytest.raises(ValueError, match=re.escape(message)):
        task_registry.create(
            f"child-of-{closed_status}", "detail", parent=parent.id, operation_key=str(uuid4())
        )
    # Nothing leaked: the rejected create is fully rolled back.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_tasks WHERE title = %s",
            (f"child-of-{closed_status}",),
        )
        assert cur.fetchone()[0] == 0  # type: ignore[index]


def test_create_rejects_parent_1_when_not_root(db_conn: psycopg.Connection) -> None:
    """The documented root id (1) is enforced: on a deployment where task 1 is
    not the system root, create(parent=1) fails loudly instead of silently
    attaching a top-level task under a different parent."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    # Self-contained: pin task 1 (non-root) and task 2 (the root), so the
    # assertion does not depend on the fixture-seeded root's id.
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM agent_tasks")
        cur.execute(
            "INSERT INTO agent_tasks (id, title, description, status, created_by, owner, is_root) "
            "VALUES (1, 'not-root', 'd', 'in_progress', %s, %s, FALSE), "
            "(2, 'Root', 'root', 'in_progress', 'system', NULL, TRUE)",
            (str(agent_id), agent_id),
        )
    db_conn.commit()
    with pytest.raises(ValueError, match="not the system root task"):
        task_registry.create("wants-root", "d", parent=1, operation_key=str(uuid4()))
    # The actual root id works as a top-level parent.
    task = task_registry.create("top-level", "d", parent=2, operation_key=str(uuid4()))
    assert task.parent_id == 2


def test_update_reparents_under_new_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        "parent-task", "d", parent=root_task_id, operation_key=str(uuid4())
    )
    child = task_registry.create("child-task", "d", parent=root_task_id, operation_key=str(uuid4()))
    # Both tasks are top-level (parent=root); reparent the child under `parent`.
    task_registry.update(child.id, parent_id=parent.id, operation_key=str(uuid4()))
    assert _persisted_parent(db_conn, child.id) == parent.id


def test_update_parent_none_moves_to_root(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        "parent-task2", "d", parent=root_task_id, operation_key=str(uuid4())
    )
    child = task_registry.create(
        "child-task2", "d", parent=root_task_id, operation_key=str(uuid4())
    )
    task_registry.update(child.id, parent_id=parent.id, operation_key=str(uuid4()))
    assert _persisted_parent(db_conn, child.id) == parent.id
    # Explicit None = back under the system root (the same anchor create()
    # callers pass explicitly for top-level tasks).
    task_registry.update(child.id, parent_id=None, operation_key=str(uuid4()))
    assert _persisted_parent(db_conn, child.id) == root_task_id


def test_update_rejects_self_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("self-parent", "d", parent=root_task_id, operation_key=str(uuid4()))
    with pytest.raises(ValueError, match="own parent"):
        task_registry.update(task.id, parent_id=task.id, operation_key=str(uuid4()))


def test_update_rejects_missing_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "missing-parent", "d", parent=root_task_id, operation_key=str(uuid4())
    )
    with pytest.raises(ValueError, match="does not exist"):
        task_registry.update(task.id, parent_id=999_999, operation_key=str(uuid4()))


@pytest.mark.parametrize("closed_status", ["done", "cancelled"])
def test_update_rejects_closed_parent(
    db_conn: psycopg.Connection, closed_status: str, root_task_id: int
) -> None:
    """Reparenting a task under a closed (done / cancelled) parent is rejected —
    a closed task never gains children, the same invariant as create() (task #1975)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        f"reparent-parent-{closed_status}",
        "detail",
        parent=root_task_id,
        operation_key=str(uuid4()),
    )
    child = task_registry.create(
        f"reparent-child-{closed_status}", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    task_registry.update(parent.id, status=closed_status, operation_key=str(uuid4()))
    message = (
        f"task {child.id} cannot be moved under a closed parent #{parent.id} ({closed_status})"
    )
    with pytest.raises(ValueError, match=re.escape(message)):
        task_registry.update(child.id, parent_id=parent.id, operation_key=str(uuid4()))
    # The tree is unchanged: the child stays under the root.
    assert _persisted_parent(db_conn, child.id) == root_task_id


def test_create_serializes_against_concurrent_parent_close(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """TOCTOU guard (QA #993): create() locks the parent row FOR UPDATE, and the
    close path holds the same lock — so a concurrent close cannot commit between
    the status read and the child INSERT and leave a child under a closed parent."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    outcome: dict[str, str] = {}

    def worker() -> None:
        try:
            task_registry.create(
                "race-child", "detail", parent=parent_id, operation_key=str(uuid4())
            )
        except ValueError as exc:
            outcome["err"] = str(exc)

    parent = task_registry.create(
        "race-parent", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    parent_id = parent.id
    # Mimic the close path: UPDATE the parent to done but hold the row lock
    # uncommitted — the worker's parent-row SELECT FOR UPDATE must block.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_tasks SET status = 'done' WHERE id = %s", (parent_id,))
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive(), "create did not block on the parent row lock"
    db_conn.commit()  # the concurrent close lands: parent is now done
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "closed task cannot be the parent" in outcome["err"]


def test_update_reparent_serializes_against_concurrent_parent_close(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """The reparent path locks the parent row FOR UPDATE too (QA #993), so a
    concurrent close cannot land between the status read and the parent_id
    UPDATE."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    outcome: dict[str, str] = {}

    def worker() -> None:
        try:
            task_registry.update(child_id, parent_id=parent_id, operation_key=str(uuid4()))
        except ValueError as exc:
            outcome["err"] = str(exc)

    parent = task_registry.create(
        "race-parent2", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    child = task_registry.create(
        "race-child2", "detail", parent=root_task_id, operation_key=str(uuid4())
    )
    parent_id, child_id = parent.id, child.id
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_tasks SET status = 'done' WHERE id = %s", (parent_id,))
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive(), "reparent did not block on the parent row lock"
    db_conn.commit()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "closed parent" in outcome["err"]
    # The tree is unchanged: the child stays under the root.
    assert _persisted_parent(db_conn, child_id) == root_task_id


def test_update_rejects_cycle_under_own_descendant(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    a = task_registry.create("cycle-a", "d", parent=root_task_id, operation_key=str(uuid4()))
    b = task_registry.create("cycle-b", "d", parent=root_task_id, operation_key=str(uuid4()))
    c = task_registry.create("cycle-c", "d", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(b.id, parent_id=a.id, operation_key=str(uuid4()))
    task_registry.update(c.id, parent_id=b.id, operation_key=str(uuid4()))
    # a -> c would put a under its own descendant: rejected.
    with pytest.raises(ValueError, match="descendant"):
        task_registry.update(a.id, parent_id=c.id, operation_key=str(uuid4()))
    # The tree is unchanged.
    assert _persisted_parent(db_conn, b.id) == a.id
    assert _persisted_parent(db_conn, c.id) == b.id


def test_update_parent_alone_is_a_valid_update(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A pure reparent (no other field) must not hit the nothing-to-update guard."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(
        "lone-parent", "d", parent=root_task_id, operation_key=str(uuid4())
    )
    child = task_registry.create("lone-child", "d", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.update(child.id, parent_id=parent.id, operation_key=str(uuid4()))
    assert _persisted_parent(db_conn, child.id) == parent.id


def test_sdk_task_timestamps_are_bare_cluster_zone(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """Issue #181: the SDK task object must not mix timestamp conventions.

    `created_at` / `updated_at` / `last_reminded_at` are agent-facing rendered
    timestamps, so they carry the bare cluster-zone form (no UTC/offset suffix)
    — the same convention as the `results` notes. The gateway JSON API and the
    DB keep explicit offsets; only this object is uniform."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("ts-uniform", "d", parent=root_task_id, operation_key=str(uuid4()))
    task_registry.log(task.id, "probe log line 1", operation_key=str(uuid4()))
    got = task_registry.get(task.id)

    for field in ("created_at", "updated_at"):
        value = getattr(got, field)
        assert isinstance(value, str), f"{field} must be a rendered string"
        # Bare cluster-zone: bracket format, no suffix of any kind.
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]$", value), (
            f"{field} is not a bare timestamp: {value!r}"
        )
        assert "+00:00" not in value and "+08" not in value and "Z" not in value

    # last_reminded_at is None before any reminder (still typed as rendered
    # string once set — same convention).
    assert got.last_reminded_at is None

    # The results notes use the same bare convention — one object, one
    # convention end to end.
    assert got.results is not None
    for line in got.results.splitlines():
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line), (
            f"note line is not bare: {line!r}"
        )
