"""Integration tests for GET /api/agents/{id}/inspect/widgets (task #2909).

The extension surface where enabled plugins embed inspector widgets: the
gateway builds the widget registry in process (mocked here by patching
`_load_inspect_widgets`), resolves the closed target vocabulary for the agent
— its open notice (the same read as /inspect/live) and the queue's
task-ownership rule — and projects each widget, dropping unresolved buttons
and emptied widgets.

Locks: empty registry -> [], unknown agent -> 404, the notice/task resolution
rules (named task wins, first real task fallback, no notice/no task drop the
button), widget ordering/attribution passthrough, the loader's enabled-set
filtering, and its fail-soft skip of a broken inspector.py.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import _plugin_inspector
from shared.plugin_context import PluginContext
from shared.plugin_inspector import (
    InspectButtonSpec,
    InspectWidgetSpec,
    clear_registry,
    register_inspect_widget,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    clear_registry()
    yield
    clear_registry()


def _widget(**over: Any) -> InspectWidgetSpec:
    data: dict[str, Any] = {
        "id": "jump-buttons",
        "kind": "jumpButtons",
        "order": 50,
        "buttons": [
            InspectButtonSpec(target="notice"),
            InspectButtonSpec(target="task"),
        ],
    }
    data.update(over)
    with PluginContext("ava_fleet"):
        return register_inspect_widget(InspectWidgetSpec(**data))


def _patch_loader(monkeypatch: pytest.MonkeyPatch, *specs: InspectWidgetSpec) -> None:
    """Seed the in-process loader with the given registry rows — the loader
    itself is covered by the tests at the bottom of this file."""

    def _loaded() -> list[InspectWidgetSpec]:
        return list(specs)

    monkeypatch.setattr(_plugin_inspector, "_load_inspect_widgets", _loaded)


def _insert_agent(db: psycopg.Connection, label: str = "t") -> int:
    """INSERT an agents row + its agents_meta row (the /inspect family checks
    agents_meta; a bare `agents` row would 404)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    tid = row[0]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (tid,),
        )
    return tid


def _insert_notice(
    db: psycopg.Connection,
    agent_id: int,
    *,
    task_id: int | None = None,
    require_response: bool = True,
    title: str = "notice",
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices "
            "(local_id, agent_id, title, content, priority, require_response, blocking, task_id, expire_at) "
            "VALUES (1, %s, %s, NULL, 'P2', %s, FALSE, %s, now() + interval '1 day') "
            "RETURNING id",
            (agent_id, title, require_response, task_id),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _root_task_id(db: psycopg.Connection) -> int:
    """The system root's id for use as a parent (the throwaway test DB may or
    may not carry the seeded root row)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM agent_tasks WHERE is_root = TRUE LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
                "VALUES ('Root', 'd', 'ongoing', 'system', TRUE) RETURNING id"
            )
            row = cur.fetchone()
    assert row is not None
    return row[0]


def _insert_task(
    db: psycopg.Connection,
    *,
    owner: int | None,
    parent_id: int | None,
    title: str,
    created_seconds_ago: float = 0,
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks "
            "(parent_id, title, description, status, owner, created_by, created_at, updated_at) "
            "VALUES (%s, %s, 'd', 'in_progress', %s, %s, "
            "now() - make_interval(secs => %s), now()) RETURNING id",
            (
                parent_id,
                title,
                owner,
                str(owner) if owner is not None else "user",
                created_seconds_ago,
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _get(aid: int) -> Any:
    with TestClient(app) as client:
        return client.get(f"/api/agents/{aid}/inspect/widgets")


# ── empty registry / unknown agent ────────────────────────────────────────────


def test_empty_registry_returns_empty(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_loader(monkeypatch)
    aid = _insert_agent(db_conn)
    db_conn.commit()
    resp = _get(aid)
    assert resp.status_code == 200
    assert resp.json() == []


def test_unknown_agent_404(db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """No agents_meta row -> 404 (same contract as /inspect)."""
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()
    assert _get(999999).status_code == 404


# ── resolution ────────────────────────────────────────────────────────────────


def test_resolves_notice_and_its_named_task(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    task = _insert_task(db_conn, owner=aid, parent_id=_root_task_id(db_conn), title="named")
    _insert_notice(db_conn, aid, task_id=task)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    resp = _get(aid)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    widget = body[0]
    assert (widget["plugin"], widget["id"], widget["kind"], widget["order"]) == (
        "ava_fleet",
        "jump-buttons",
        "jumpButtons",
        50,
    )
    assert widget["buttons"][0]["target"] == "notice"
    assert widget["buttons"][0]["notice_id"] is not None
    assert widget["buttons"][1]["target"] == "task"
    # The notice's named task wins over the agent's task list.
    assert widget["buttons"][1]["task_id"] == task


def test_task_falls_back_to_first_real_task_owned_by_agent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn, label="other")
    # Older task of ours, a NEWER task of someone else, and our newest — the
    # fallback is our newest real task (created_at DESC, like the console's
    # taskForAgent over the created_at-desc list).
    root = _root_task_id(db_conn)
    mine_old = _insert_task(
        db_conn, owner=aid, parent_id=root, title="mine old", created_seconds_ago=600
    )
    _insert_task(db_conn, owner=other, parent_id=root, title="other's", created_seconds_ago=300)
    mine_new = _insert_task(
        db_conn, owner=aid, parent_id=root, title="mine new", created_seconds_ago=100
    )
    _insert_notice(db_conn, aid, task_id=None)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    body = _get(aid).json()
    buttons = {b["target"]: b for b in body[0]["buttons"]}
    assert buttons["task"]["task_id"] == mine_new != mine_old


def test_root_task_is_not_a_task_target(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `parent_id IS NULL` row (the system root) never becomes the target."""
    aid = _insert_agent(db_conn)
    root = _insert_task(db_conn, owner=aid, parent_id=None, title="root-ish")
    _insert_notice(db_conn, aid, task_id=None)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    body = _get(aid).json()
    # The only candidate is a root row -> no task button -> the notice-only
    # button list survives only because the notice resolved; assert the task
    # target is absent.
    task_buttons = [b for b in body[0]["buttons"] if b["target"] == "task"]
    assert task_buttons == []
    assert body[0]["buttons"][0]["target"] == "notice"
    assert root  # the row exists; it was simply not eligible


def test_named_task_wins_even_when_owned_by_another_agent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console's rule is `byId.get(named) ?? taskForAgent` — no owner
    check — so a notice naming someone else's task jumps there, not to the
    agent's own first task. (The FK keeps the named id always real; the
    fallback covers a notice with no task at all.)"""
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn, label="other")
    root = _root_task_id(db_conn)
    theirs = _insert_task(db_conn, owner=other, parent_id=root, title="theirs")
    mine = _insert_task(db_conn, owner=aid, parent_id=root, title="mine")
    _insert_notice(db_conn, aid, task_id=theirs)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    body = _get(aid).json()
    buttons = {b["target"]: b for b in body[0]["buttons"]}
    assert buttons["task"]["task_id"] == theirs != mine


def test_no_notice_drops_notice_button(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    task = _insert_task(db_conn, owner=aid, parent_id=_root_task_id(db_conn), title="only task")
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    body = _get(aid).json()
    assert [b["target"] for b in body[0]["buttons"]] == ["task"]
    assert body[0]["buttons"][0]["task_id"] == task


def test_emptied_widget_drops_out_of_the_response(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither target resolves -> the widget is not in the payload (the
    panel's empty-section rule, applied server-side)."""
    aid = _insert_agent(db_conn)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    assert _get(aid).json() == []


def test_notice_only_widget_keeps_only_its_resolved_button(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    _insert_notice(db_conn, aid)
    _patch_loader(
        monkeypatch,
        _widget(id="notice-only", buttons=[InspectButtonSpec(target="notice")]),
    )
    db_conn.commit()

    body = _get(aid).json()
    assert len(body) == 1
    assert [b["target"] for b in body[0]["buttons"]] == ["notice"]


def test_fyi_notice_within_ttl_resolves(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice read matches the live inspect's predicate: an unexpired
    require_response notice resolves (an FYI older than its TTL would not)."""
    aid = _insert_agent(db_conn)
    _insert_notice(db_conn, aid, require_response=False)
    _patch_loader(monkeypatch, _widget())
    db_conn.commit()

    body = _get(aid).json()
    assert [b["target"] for b in body[0]["buttons"]] == ["notice"]


def test_widgets_keep_registration_order(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    _insert_notice(db_conn, aid)
    _patch_loader(
        monkeypatch,
        _widget(id="second", order=750),
        _widget(id="first", order=10),
    )
    db_conn.commit()

    body = _get(aid).json()
    assert [w["id"] for w in body] == ["second", "first"]
    assert [w["order"] for w in body] == [750, 10]


# ── the loader ────────────────────────────────────────────────────────────────


def _shipped_fleet_module() -> Any:
    path = _plugin_inspector._PLUGINS_DIR / "ava_fleet" / "inspector.py"
    assert path.is_file(), "ava_fleet must ship inspector.py for this test"
    return path


def _seed_fleet_widget() -> None:
    """Re-run the shipped module's registration under its plugin context —
    module caching means a plain import after clear_registry() would not
    register again (same dance as the plugin-metric loader test)."""
    mod_name = "ava_builtins.plugins.ava_fleet.inspector"
    mod = sys.modules.get(mod_name)
    with PluginContext("ava_fleet"):
        if mod is None:
            importlib.import_module(mod_name)
        else:
            importlib.reload(mod)


def test_loader_imports_shipped_fleet_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _shipped_fleet_module()
    _seed_fleet_widget()
    monkeypatch.setattr(_plugin_inspector, "_enabled_inspector_modules", lambda: [module])

    specs = _plugin_inspector._load_inspect_widgets()
    assert [(s.plugin, s.id, s.kind, s.order) for s in specs] == [
        ("ava_fleet", "jump-buttons", "jumpButtons", 50)
    ]
    assert [b.target for b in specs[0].buttons] == ["notice", "task"]


def test_loader_filters_widgets_of_disabled_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin disabled after its module was imported keeps its registration
    object in the process registry but must not serve widgets."""
    _seed_fleet_widget()

    def _no_modules() -> list[Any]:
        return []

    monkeypatch.setattr(_plugin_inspector, "_enabled_inspector_modules", _no_modules)
    assert _plugin_inspector._load_inspect_widgets() == []


def test_enabled_modules_skips_a_disabled_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_enabled_inspector_modules` reads the enable bit per request."""
    from shared import plugins_config

    module = _shipped_fleet_module()
    monkeypatch.setattr(
        plugins_config,
        "installed_plugin_dirs",
        lambda: {"ava_fleet": module.parent},
    )

    def _loader(config: plugins_config.PluginsConfig) -> Any:
        def _load(_known: set[str]) -> plugins_config.PluginsConfig:
            return config

        return _load

    enabled = plugins_config.PluginsConfig(
        plugins={"ava_fleet": plugins_config.PluginEntry(enabled=True)}
    )
    monkeypatch.setattr(plugins_config, "load_for_runtime", _loader(enabled))
    assert _plugin_inspector._enabled_inspector_modules() == [module]

    disabled = plugins_config.PluginsConfig(
        plugins={"ava_fleet": plugins_config.PluginEntry(enabled=False)}
    )
    monkeypatch.setattr(plugins_config, "load_for_runtime", _loader(disabled))
    assert _plugin_inspector._enabled_inspector_modules() == []


def test_loader_skips_a_plugin_whose_inspector_fails_to_import(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """Fail-soft (user ruling 2026-09-11): a broken inspector.py is reported
    loudly and skipped — the endpoint keeps serving the remaining widgets.

    The 2026-08-28 ava_ledger incident shape (a sibling import that blows up
    at module import) restated for the inspector surface: the load must not
    raise, the remaining plugin still serves, and the failure is loud on both
    channels (loguru ERROR + the plugin_load_failed telemetry event)."""
    import shared.telemetry

    good = _shipped_fleet_module()
    bad = _plugin_inspector._PLUGINS_DIR / "broken_plugin" / "inspector.py"
    _seed_fleet_widget()
    monkeypatch.setattr(_plugin_inspector, "_enabled_inspector_modules", lambda: [bad, good])

    events: list[tuple[str, dict[str, object]]] = []

    def fake_emit(
        category: str,
        event_name: str,
        *,
        level: str = "info",
        attributes: dict[str, object] | None = None,
        **kw: object,
    ) -> None:
        events.append((event_name, attributes or {}))

    monkeypatch.setattr(shared.telemetry, "emit", fake_emit)

    real_import_module = importlib.import_module

    def fake_import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ava_builtins.plugins.broken_plugin.inspector":
            raise ModuleNotFoundError("No module named '_missing'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(
        _plugin_inspector, "importlib", SimpleNamespace(import_module=fake_import_module)
    )

    # A half-executed module must not survive the failed import.
    leftover = "ava_builtins.plugins.broken_plugin.inspector"
    sys.modules[leftover] = ModuleType(leftover)
    try:
        specs = _plugin_inspector._load_inspect_widgets()  # must not raise
        assert leftover not in sys.modules
    finally:
        sys.modules.pop(leftover, None)

    # the healthy plugin still serves; the broken one contributes nothing
    assert [(s.plugin, s.id) for s in specs] == [("ava_fleet", "jump-buttons")]
    # loud: a loguru error naming the plugin
    assert any(
        "broken_plugin" in r["message"] and "fail-soft" in r["message"] for r in loguru_records
    )
    # loud: the plugin_load_failed event carrying the plugin + the exception
    attrs = [a for n, a in events if n == "plugin_load_failed"]
    assert [a["plugin"] for a in attrs] == ["broken_plugin"]
    assert "ModuleNotFoundError" in str(attrs[0]["error"])

    # every plugin broken -> still no raise, an empty registry
    monkeypatch.setattr(_plugin_inspector, "_enabled_inspector_modules", lambda: [bad])
    assert _plugin_inspector._load_inspect_widgets() == []


def test_loader_drops_partial_widget_registrations_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """An inspector.py that raises after registering leaves nothing behind:
    the loader drops the dying attempt's widgets, so a fixed file recovers on
    the next request instead of dying on DuplicateInspectWidget
    (fail-soft, user ruling 2026-09-11)."""
    from shared.plugin_inspector import registered_inspect_widgets

    plugin_dir = tmp_path / "drop_partial_insp"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    inspector_py = plugin_dir / "inspector.py"
    source = (
        "from shared.plugin_inspector import InspectButtonSpec, InspectWidgetSpec, "
        "register_inspect_widget\n"
        "register_inspect_widget(InspectWidgetSpec(id='drop_partial_widget', "
        "kind='jumpButtons', order=50, "
        "buttons=[InspectButtonSpec(target='notice')]))\n"
    )
    inspector_py.write_text(source + "raise RuntimeError('inspector boom')\n", encoding="utf-8")

    monkeypatch.setattr(_plugin_inspector, "_enabled_inspector_modules", lambda: [inspector_py])
    shipped_path = importlib.import_module("ava_builtins.plugins").__path__
    monkeypatch.setattr("ava_builtins.plugins.__path__", [*shipped_path, str(tmp_path)])

    assert _plugin_inspector._load_inspect_widgets() == []  # must not raise
    assert [s for s in registered_inspect_widgets() if s.plugin == "drop_partial_insp"] == []
    assert any(
        "drop_partial_insp" in r["message"] and "failed to load" in r["message"]
        for r in loguru_records
    )

    inspector_py.write_text(source, encoding="utf-8")
    specs = _plugin_inspector._load_inspect_widgets()
    assert [(s.plugin, s.id) for s in specs] == [("drop_partial_insp", "drop_partial_widget")]
