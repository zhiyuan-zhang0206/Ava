"""Landing cluster extensions on a machine — `shared/extension_materialize.py`.

Slice S2 of `future/infra/extension-ownership.md`. The contract is a three-way
verdict per extension, and the third case is the one worth the file:

1. **absent** -> extract. The catch-up that makes a machine fungible.
2. **matching** -> nothing. Must not rewrite on every converge.
3. **different** -> either the cluster row moved (re-extract) or a person edited
   this machine's copy (**refuse**). Getting this backwards either destroys
   someone's in-place work or pins a machine to a stale version forever.

Plus: a repo-source row never materializes (checkout content does not ride the
data plane), and a failed extraction does not leave the name empty.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from shared import extension_materialize as mat
from shared import extension_registry as reg


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _returns(value: str | None) -> Callable[[str], str | None]:
    """A stand-in for `_last_written_hash` — what this machine last wrote.

    A named factory rather than a lambda so the stub carries the real
    signature; an untyped lambda would type-check as accepting anything.
    """

    def _stub(_name: str) -> str | None:
        return value

    return _stub


def _register(conn: psycopg.Connection, name: str, root: Path, *, source: str = "https://x.inv"):
    """Put `root`'s content in the registry under `name`; return its tree hash."""
    from shared.install_registry import tree_hash

    archive = reg.pack_tree(root)
    digest = tree_hash(root)
    reg.put_blob(conn, archive, name=name, content_hash=digest)
    reg.upsert(conn, name=name, kind="skill", source=source, content_hash=digest)
    return digest


class TestTheThreeVerdicts:
    def test_an_absent_skill_is_landed(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        src = _tree(tmp_path / "src", {"SKILL.md": "body", "ref/x.md": "deep"})
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", src)
            result = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert result.landed == ["alpha"]
        assert (dest_root / "alpha" / "SKILL.md").read_text() == "body"
        assert (dest_root / "alpha" / "ref/x.md").read_text() == "deep"

    def test_a_matching_skill_is_left_alone(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """Converge runs on every `ava start`; rewriting an unchanged tree every
        time would churn mtimes and make "did anything change" unanswerable."""
        src = _tree(tmp_path / "src", {"SKILL.md": "body"})
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", src)
            mat.materialize_skills(db_conn, dest_root=dest_root)
            second = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert second.unchanged == ["alpha"]
        assert second.landed == []
        assert second.updated == []

    def test_a_moved_row_updates_an_untouched_copy(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Someone installed a new version on another machine. This machine's
        copy is untouched since it was written, so it follows."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            v1 = _register(db_conn, "alpha", _tree(tmp_path / "v1", {"SKILL.md": "one"}))
            mat.materialize_skills(db_conn, dest_root=dest_root)
            # This machine last wrote v1 — what the per-machine registry records.
            monkeypatch.setattr(mat, "_last_written_hash", _returns(v1))

            _register(db_conn, "alpha", _tree(tmp_path / "v2", {"SKILL.md": "two"}))
            result = mat.materialize_skills(db_conn, dest_root=dest_root)

        assert result.updated == ["alpha"]
        assert (dest_root / "alpha" / "SKILL.md").read_text() == "two"

    def test_a_local_edit_is_never_overwritten(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one that matters. The local tree differs from the row AND from
        what this machine last wrote, so a person changed it — silently
        reverting would destroy in-place work with no trace."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            v1 = _register(db_conn, "alpha", _tree(tmp_path / "v1", {"SKILL.md": "one"}))
            mat.materialize_skills(db_conn, dest_root=dest_root)
            monkeypatch.setattr(mat, "_last_written_hash", _returns(v1))

            (dest_root / "alpha" / "SKILL.md").write_text("MY EDIT")
            _register(db_conn, "alpha", _tree(tmp_path / "v2", {"SKILL.md": "two"}))
            result = mat.materialize_skills(db_conn, dest_root=dest_root)

        assert result.kept_local_edits == ["alpha"]
        assert result.updated == []
        assert (dest_root / "alpha" / "SKILL.md").read_text() == "MY EDIT"

    def test_an_untracked_local_copy_is_treated_as_edited(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No record of what this machine wrote = no evidence the tree is
        untouched. Refusing is the conservative direction: the cost is a manual
        resolve, versus destroying an edit nobody can recover."""
        dest_root = tmp_path / "skills"
        _tree(dest_root / "alpha", {"SKILL.md": "pre-existing, provenance unknown"})
        monkeypatch.setattr(mat, "_last_written_hash", _returns(None))
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", _tree(tmp_path / "v1", {"SKILL.md": "cluster version"}))
            result = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert result.kept_local_edits == ["alpha"]
        assert (dest_root / "alpha" / "SKILL.md").read_text() == "pre-existing, provenance unknown"


class TestWhatItRefusesToTouch:
    def test_a_repo_row_is_never_materialized(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """Checkout content stays cluster-consistent via commit-pinned rollout
        and keeps converging from the repo; it must not also arrive here."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            reg.upsert(
                db_conn,
                name="repo-skill",
                kind="skill",
                source=reg.REPO_SOURCE,
                content_hash=None,
            )
            result = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert result.landed == []
        assert not (dest_root / "repo-skill").exists()

    def test_a_disabled_row_is_not_landed(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", _tree(tmp_path / "src", {"SKILL.md": "body"}))
            reg.set_default_enabled(db_conn, "alpha", enabled=False)
            result = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert result.landed == []
        assert not (dest_root / "alpha").exists()

    def test_a_plugin_row_is_not_landed_by_the_skill_pass(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """S2 is skills only — a plugin row must wait for its own slice rather
        than being half-materialized by this one."""
        dest_root = tmp_path / "skills"
        src = _tree(tmp_path / "src", {"plugin.py": "x = 1"})
        with db_conn.transaction(force_rollback=True):
            from shared.install_registry import tree_hash

            digest = tree_hash(src)
            reg.put_blob(db_conn, reg.pack_tree(src), name="p", content_hash=digest)
            reg.upsert(
                db_conn, name="p", kind="plugin", source="https://x.inv", content_hash=digest
            )
            result = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert result.landed == []
        assert not (dest_root / "p").exists()


class TestFailureStates:
    def test_a_row_whose_blob_is_gone_is_reported_not_raised(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """One broken row must not stop every other skill from landing. The FK
        makes this state unreachable through the module; the check is what keeps
        a hand-written row from taking the pass down."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "good", _tree(tmp_path / "good", {"SKILL.md": "ok"}))
            # Bypass the FK the way only a direct write can, to reach the state.
            db_conn.execute("ALTER TABLE extensions DROP CONSTRAINT extensions_content_hash_fkey")
            reg.upsert(
                db_conn,
                name="broken",
                kind="skill",
                source="https://x.inv",
                content_hash="hash-with-no-blob",
            )
            result = mat.materialize_skills(db_conn, dest_root=dest_root)

        assert result.missing_blob == ["broken"]
        assert result.landed == ["good"], "a broken row must not block a healthy one"

    def test_dry_run_classifies_without_writing(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", _tree(tmp_path / "src", {"SKILL.md": "body"}))
            result = mat.materialize_skills(db_conn, dest_root=dest_root, dry_run=True)
        assert result.landed == ["alpha"]
        assert not (dest_root / "alpha").exists()

    def test_replacing_leaves_no_staging_directories_behind(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The swap stages a sibling and renames; a leftover `.incoming` or
        `.previous` would be picked up as a skill directory by the scanner."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            v1 = _register(db_conn, "alpha", _tree(tmp_path / "v1", {"SKILL.md": "one"}))
            mat.materialize_skills(db_conn, dest_root=dest_root)
            monkeypatch.setattr(mat, "_last_written_hash", _returns(v1))
            _register(db_conn, "alpha", _tree(tmp_path / "v2", {"SKILL.md": "two"}))
            mat.materialize_skills(db_conn, dest_root=dest_root)

        assert sorted(p.name for p in dest_root.iterdir()) == ["alpha"]


class TestChangedFlag:
    def test_changed_is_true_only_when_something_landed(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """The caller uses it to decide whether to say anything; an unchanged
        pass should be silent."""
        dest_root = tmp_path / "skills"
        with db_conn.transaction(force_rollback=True):
            _register(db_conn, "alpha", _tree(tmp_path / "src", {"SKILL.md": "body"}))
            first = mat.materialize_skills(db_conn, dest_root=dest_root)
            second = mat.materialize_skills(db_conn, dest_root=dest_root)
        assert first.changed is True
        assert second.changed is False
