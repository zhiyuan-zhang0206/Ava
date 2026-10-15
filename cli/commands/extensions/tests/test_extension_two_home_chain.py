from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import psycopg

from base import paths
from base.cluster.machine import MachineIdentity
from base.db.code_version_gate import ProcessDbGate
from base.packages.extensions import materialize as mat
from base.packages.extensions import registry as reg
from cli.commands.extensions.skill import cmd_skill_install
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database

_AsMachine = Callable[[Path], AbstractContextManager[MachineIdentity]]

_SKILL_MD = """---
name: {name}
description: a test skill, use when exercising the two-home chain
---

# {name}

{body}
"""


def _write_skill(d: Path, name: str, body: str = "Original instructions.") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(name=name, body=body), encoding="utf-8")
    (d / "references").mkdir(exist_ok=True)
    (d / "references" / "REFERENCE.md").write_text(f"# {name} reference\n", encoding="utf-8")
    return d


def test_install_on_home_a_materializes_on_home_b(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    src = _write_skill(tmp_path / "src" / "two-home-demo", "two-home-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with as_machine(home_a) as installing_machine:
        assert (
            cmd_skill_install(
                str(src),
                None,
                None,
                database_factory=operator_database,
                machine=installing_machine,
            )
            == 0
        )
        assert (home_a / "skills" / "two-home-demo" / "SKILL.md").is_file()

    with as_machine(home_b):
        assert not (home_b / "skills" / "two-home-demo").exists(), (
            "home B must start without it, or the test proves nothing"
        )
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))

        assert result.landed == ["two-home-demo"]
        landed = home_b / "skills" / "two-home-demo"
        assert (landed / "SKILL.md").read_text(encoding="utf-8") == (src / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert (landed / "references" / "REFERENCE.md").is_file(), (
            "the whole tree crosses, not just the entry file"
        )


def test_home_b_is_idempotent_on_a_second_pass(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    src = _write_skill(tmp_path / "src" / "idem-demo", "idem-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with as_machine(home_a) as installing_machine:
        assert (
            cmd_skill_install(
                str(src),
                None,
                None,
                database_factory=operator_database,
                machine=installing_machine,
            )
            == 0
        )

    with as_machine(home_b):
        first = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))
        second = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))

    assert first.landed == ["idem-demo"]
    assert second.unchanged == ["idem-demo"]
    assert second.landed == [] and second.updated == []


def test_the_row_records_the_installing_machine(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    src = _write_skill(tmp_path / "src" / "provenance-demo", "provenance-demo")
    home_a = tmp_path / "home-a"

    with as_machine(home_a) as installing_machine:
        assert (
            cmd_skill_install(
                str(src),
                None,
                None,
                database_factory=operator_database,
                machine=installing_machine,
            )
            == 0
        )

    row = reg.get(db_conn, "provenance-demo")
    assert row is not None
    assert row.source == f"local:{home_a.name}"
    assert row.kind == "skill"
    assert row.trust == "unreviewed", "third-party content is never auto-trusted"
    assert row.content_hash is not None, "an installed row must carry its content"


def test_disabling_on_the_cluster_stops_new_machines_getting_it(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    src = _write_skill(tmp_path / "src" / "policy-demo", "policy-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with as_machine(home_a) as installing_machine:
        assert (
            cmd_skill_install(
                str(src),
                None,
                None,
                database_factory=operator_database,
                machine=installing_machine,
            )
            == 0
        )

    assert reg.set_default_enabled(db_conn, "policy-demo", enabled=False) is True
    db_conn.commit()

    with as_machine(home_b):
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))

    assert result.landed == []
    assert not (home_b / "skills" / "policy-demo").exists()


def test_home_b_keeps_its_own_edit_when_the_cluster_moves(
    tmp_path: Path,
    as_machine: _AsMachine,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    database_gate: ProcessDbGate,
    *,
    command_runtime: CommandRuntime,
) -> None:
    src = _write_skill(tmp_path / "src" / "edit-demo", "edit-demo", body="Version one.")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with as_machine(home_a) as installing_machine:
        assert (
            cmd_skill_install(
                str(src),
                None,
                None,
                database_factory=operator_database,
                machine=installing_machine,
            )
            == 0
        )

    with as_machine(home_b):
        assert mat.materialize_skills(
            db_conn, dest_root=paths.skills_dir(paths.ava_home())
        ).landed == ["edit-demo"]
        edited = home_b / "skills" / "edit-demo" / "SKILL.md"
        edited.write_text(
            _SKILL_MD.format(name="edit-demo", body="MY LOCAL EDIT"), encoding="utf-8"
        )

    newer = _write_skill(tmp_path / "src2" / "edit-demo", "edit-demo", body="Version two.")
    with as_machine(home_a) as installing_machine:
        from base import db as base_db

        with base_db.pool(gate=database_gate) as registry_pool:
            reg.register_tree(
                registry_pool,
                root=newer,
                name="edit-demo",
                kind="skill",
                source=f"local:{installing_machine.name()}",
            )

    with as_machine(home_b):
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir(paths.ava_home()))

    assert result.kept_local_edits == ["edit-demo"]
    assert result.updated == []
    assert "MY LOCAL EDIT" in (home_b / "skills" / "edit-demo" / "SKILL.md").read_text(
        encoding="utf-8"
    )
