"""The S2 headline lock: install on home A, materialize on home B, one Postgres.

`future/infra/extension-ownership.md`'s own S2 test lock is *"install on home A
materializes on home B (two homes, one PG)"*, and it is the entire claim the
slice exists to make: the cluster owns extensions, so a machine that never ran
the install still ends up with the content.

Every other test in the slice passes whether or not that chain works. The
registry tests cover rows and blobs; the materializer tests cover the verdicts;
the install tests cover the local copy. All of them are satisfied by a machine
talking to itself. This file is the only one that puts a second machine on the
other end.

## How two machines are simulated honestly

A "machine" here is its `$AVA_HOME`, which is what every per-machine fact is
derived from: `skills_dir()`, the `installed.json` install registry, and
`machine_name` all hang off it. Pointing `settings.general.ava_home` at a second
directory and resetting the cached machine identity therefore gives a genuinely
distinct machine as far as every code path under test is concerned — while the
Postgres URL is untouched, so both share one cluster database. That is exactly
the "two homes, one PG" shape, without needing two processes.

What it does NOT simulate: two machines racing concurrently, and network
partition between a runner and the cluster DB. Those are properties of the
deployment, not of this chain.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from cli.commands.skill import cmd_skill_install
from shared import extension_materialize as mat
from shared import extension_registry as reg
from shared import paths
from shared.config import settings

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


@contextmanager
def _as_machine(monkeypatch: pytest.MonkeyPatch, home: Path) -> Generator[Path]:
    """Run the block as the machine whose `$AVA_HOME` is `home`.

    `reset_identity()` on both edges is what makes this a different MACHINE and
    not just a different directory: `machine_name()` caches, and a stale cache
    would let home B claim to be home A — which would make the whole test pass
    for the wrong reason, since the row records `local:<machine>`.
    """
    from shared.machine import reset_identity

    home.mkdir(parents=True, exist_ok=True)
    (home / "machine_name").write_text(home.name, encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(settings.general, "ava_home", home)
        reset_identity()
        try:
            yield home
        finally:
            reset_identity()


def test_install_on_home_a_materializes_on_home_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """THE lock. Home B never ran the install and never saw the source tree.

    The only path by which its content can arrive is the cluster registry, so a
    pass here is the cross-machine claim and nothing else.
    """
    src = _write_skill(tmp_path / "src" / "two-home-demo", "two-home-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with _as_machine(monkeypatch, home_a):
        assert cmd_skill_install(str(src), None, None) == 0
        assert (home_a / "skills" / "two-home-demo" / "SKILL.md").is_file()

    with _as_machine(monkeypatch, home_b):
        assert not (home_b / "skills" / "two-home-demo").exists(), (
            "home B must start without it, or the test proves nothing"
        )
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())

        assert result.landed == ["two-home-demo"]
        landed = home_b / "skills" / "two-home-demo"
        assert (landed / "SKILL.md").read_text(encoding="utf-8") == (src / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert (landed / "references" / "REFERENCE.md").is_file(), (
            "the whole tree crosses, not just the entry file"
        )


def test_home_b_is_idempotent_on_a_second_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """Converge and `ava start` both run this repeatedly; the second pass must
    be a no-op, or every start rewrites trees and 'did anything change' stops
    meaning anything."""
    src = _write_skill(tmp_path / "src" / "idem-demo", "idem-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with _as_machine(monkeypatch, home_a):
        assert cmd_skill_install(str(src), None, None) == 0

    with _as_machine(monkeypatch, home_b):
        first = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())
        second = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())

    assert first.landed == ["idem-demo"]
    assert second.unchanged == ["idem-demo"]
    assert second.landed == [] and second.updated == []


def test_the_row_records_the_installing_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """A local-path install is `local:<machine>`, naming WHERE the content came
    from. That provenance is what the adoption sweep will need to resolve two
    machines claiming one name with different content — and it is only
    meaningful if the machine identity is really per-home."""
    src = _write_skill(tmp_path / "src" / "provenance-demo", "provenance-demo")
    home_a = tmp_path / "home-a"

    with _as_machine(monkeypatch, home_a):
        assert cmd_skill_install(str(src), None, None) == 0

    row = reg.get(db_conn, "provenance-demo")
    assert row is not None
    assert row.source == f"local:{home_a.name}"
    assert row.kind == "skill"
    assert row.trust == "unreviewed", "third-party content is never auto-trusted"
    assert row.content_hash is not None, "an installed row must carry its content"


def test_disabling_on_the_cluster_stops_new_machines_getting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """Enablement is CLUSTER policy, which is the half of the model that per-
    machine files could not express. Turning a skill off must reach a machine
    that has never seen it."""
    src = _write_skill(tmp_path / "src" / "policy-demo", "policy-demo")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with _as_machine(monkeypatch, home_a):
        assert cmd_skill_install(str(src), None, None) == 0

    assert reg.set_default_enabled(db_conn, "policy-demo", enabled=False) is True
    db_conn.commit()

    with _as_machine(monkeypatch, home_b):
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())

    assert result.landed == []
    assert not (home_b / "skills" / "policy-demo").exists()


def test_home_b_keeps_its_own_edit_when_the_cluster_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """The user-edit guard, across machines rather than within one.

    Home B materializes, someone edits B's copy in place, then home A installs a
    newer version. B must keep its edit — the L3 develop-a-plugin loop is
    exactly "edit the copy on the machine you are working on", and a silent
    revert would eat it.
    """
    src = _write_skill(tmp_path / "src" / "edit-demo", "edit-demo", body="Version one.")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"

    with _as_machine(monkeypatch, home_a):
        assert cmd_skill_install(str(src), None, None) == 0

    with _as_machine(monkeypatch, home_b):
        assert mat.materialize_skills(db_conn, dest_root=paths.skills_dir()).landed == ["edit-demo"]
        edited = home_b / "skills" / "edit-demo" / "SKILL.md"
        edited.write_text(
            _SKILL_MD.format(name="edit-demo", body="MY LOCAL EDIT"), encoding="utf-8"
        )

    # Home A ships a newer version of the same name.
    newer = _write_skill(tmp_path / "src2" / "edit-demo", "edit-demo", body="Version two.")
    with _as_machine(monkeypatch, home_a):
        from shared import db as shared_db
        from shared.machine import machine_name

        reg.register_tree(
            shared_db.pool(),
            root=newer,
            name="edit-demo",
            kind="skill",
            source=f"local:{machine_name()}",
        )

    with _as_machine(monkeypatch, home_b):
        result = mat.materialize_skills(db_conn, dest_root=paths.skills_dir())

    assert result.kept_local_edits == ["edit-demo"]
    assert result.updated == []
    assert "MY LOCAL EDIT" in (home_b / "skills" / "edit-demo" / "SKILL.md").read_text(
        encoding="utf-8"
    )
