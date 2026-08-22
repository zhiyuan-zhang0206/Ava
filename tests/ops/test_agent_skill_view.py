"""Unit tests for the runner-side per-agent command view."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb

from ava import _commands, skills
from ops import ops_cluster
from shared.db import create_agent


def _write_skill(root: Path, name: str, description: str = "project skill") -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clear_skill_sources() -> Generator[None, None, None]:
    skills.clear_skill_sources()
    yield
    skills.clear_skill_sources()


@pytest.fixture
def load_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "load"
    root.mkdir()
    _write_skill(root, "load-skill", "converged skill")
    monkeypatch.setattr(skills, "_skills_dir", lambda: root)
    monkeypatch.setattr(skills, "enabled_skill_names", lambda: {"load-skill"})
    monkeypatch.setattr(_commands, "_command_dirs", list)
    return root


def _project_with_skill(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    project_skills = repo / ".agents" / "skills"
    _write_skill(project_skills, "project-skill")
    return repo, project_skills


def _view_inputs(
    cwd: Path | None, wanted: list[str]
) -> Callable[[Any, int], tuple[Path | None, list[str]]]:
    def _inputs(_pool: Any, _agent_id: int) -> tuple[Path | None, list[str]]:
        return cwd, wanted

    return _inputs


class _OneConnectionPool:
    """Minimal daemon-pool stand-in backed by the test's real database connection."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def connection(self) -> Generator[psycopg.Connection, None, None]:
        yield self._conn


def test_agent_skill_view_inputs_read_checkpointed_cwd_and_pinned_narrowing(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The runner reads ava-code's persisted channel and the agent's two config maps."""
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, config_overlay, birth_config) VALUES (%s, 'running', %s, %s)",
        (
            agent_id,
            Jsonb({"skills_to_inject_into_system_prompt": ["overlay-skill"]}),
            Jsonb({"skills_to_inject_into_system_prompt": ["birth-skill"]}),
        ),
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"ava_code__cwd": str(tmp_path)}
    checkpoint["channel_versions"] = {"ava_code__cwd": "1", "__start__": "1"}
    saver = PostgresSaver(conn=cast(Any, db_conn))
    saver.put(
        config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
        checkpoint=checkpoint,
        metadata={"source": "input", "step": 1, "parents": {}},
        new_versions={"ava_code__cwd": "1"},
    )

    cwd, wanted = ops_cluster._agent_skill_view_inputs(_OneConnectionPool(db_conn), agent_id)

    assert cwd == tmp_path
    assert wanted == ["overlay-skill"]


def test_agent_skill_view_missing_row_is_load_dir_only(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    """A removed row has no cwd or narrowing, rather than failing the op."""
    view = ops_cluster.agent_skill_view_op(999_999_999, _OneConnectionPool(db_conn))

    assert [command.name for command in view.commands] == ["load-skill"]


def test_agent_skill_view_includes_project_skills_for_persisted_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_dir: Path
) -> None:
    """A cwd inside a git repository contributes its project-local skill command."""
    repo, _ = _project_with_skill(tmp_path)
    monkeypatch.setattr(ops_cluster, "_agent_skill_view_inputs", _view_inputs(repo, ["*"]))

    view = ops_cluster.agent_skill_view_op(42, object())

    assert {command.name for command in view.commands} >= {"load-skill", "project-skill"}


def test_agent_skill_view_without_cwd_or_repo_is_load_dir_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_dir: Path
) -> None:
    """Missing cwd and non-repository cwd never add a project-local root."""
    monkeypatch.setattr(ops_cluster, "_agent_skill_view_inputs", _view_inputs(None, ["*"]))
    missing_cwd_view = ops_cluster.agent_skill_view_op(42, object())

    monkeypatch.setattr(
        ops_cluster,
        "_agent_skill_view_inputs",
        _view_inputs(tmp_path / "not-a-repo", ["*"]),
    )
    non_repo_view = ops_cluster.agent_skill_view_op(42, object())

    assert {command.name for command in missing_cwd_view.commands} == {"load-skill"}
    assert {command.name for command in non_repo_view.commands} == {"load-skill"}


def test_agent_skill_view_clears_project_provider_after_each_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_dir: Path
) -> None:
    """One agent's project root cannot leak into the next op's command view."""
    repo, _ = _project_with_skill(tmp_path)
    monkeypatch.setattr(ops_cluster, "_agent_skill_view_inputs", _view_inputs(repo, ["*"]))
    assert "project-skill" in {
        command.name for command in ops_cluster.agent_skill_view_op(42, object()).commands
    }

    monkeypatch.setattr(ops_cluster, "_agent_skill_view_inputs", _view_inputs(None, ["*"]))
    assert "project-skill" not in {
        command.name for command in ops_cluster.agent_skill_view_op(43, object()).commands
    }


def test_agent_skill_view_tolerates_ava_code_plugin_import_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_dir: Path
) -> None:
    """An unavailable ava-code plugin degrades to the converged load-dir list."""
    repo, _ = _project_with_skill(tmp_path)
    monkeypatch.setattr(ops_cluster, "_agent_skill_view_inputs", _view_inputs(repo, ["*"]))
    monkeypatch.setitem(sys.modules, "ava_builtins.plugins.ava_code._walk", None)

    view = ops_cluster.agent_skill_view_op(42, object())

    assert {command.name for command in view.commands} == {"load-skill"}


def test_agent_skill_view_honors_per_agent_skill_narrowing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_dir: Path
) -> None:
    """A named prompt capability keeps only its corresponding skill command."""
    _write_skill(load_dir, "other-skill", "other converged skill")
    monkeypatch.setattr(skills, "enabled_skill_names", lambda: {"load-skill", "other-skill"})
    monkeypatch.setattr(
        ops_cluster,
        "_agent_skill_view_inputs",
        _view_inputs(None, ["load_skill"]),
    )

    view = ops_cluster.agent_skill_view_op(42, object())

    assert [command.name for command in view.commands] == ["load-skill"]
