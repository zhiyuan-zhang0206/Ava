"""The cluster extension registry — `shared/extension_registry.py` + its schema.

Slice S2 of `future/infra/extension-ownership.md`. What is locked here is what
the ownership model rests on:

1. **The size cap is a constraint, not a convention** — enforced in Python AND
   by the DB, at the same number, with the declared size forced to be the real
   one. This is the condition the slice was approved under, so it is the first
   thing tested.
2. **Content addressing is deterministic** — packing the same tree twice must
   give byte-identical archives, or `content_hash` addresses nothing.
3. **Repo rows never carry content** — "the registry owns what arrives by
   install, not by release" is a schema fact, and this proves the schema says so.
4. **Cluster policy survives a reinstall** — `default_enabled` is an operator's
   decision; an upgrade must not silently re-enable something turned off.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from shared import extension_registry as reg


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


class TestTheSizeCap:
    """The cap has to bite in both places, at the same number.

    A cap that lives only in Python is bypassed by any other writer; one that
    lives only in the DDL surfaces as a constraint violation from a later
    transaction instead of at install time, where the operator still has the
    tree in front of them.
    """

    def test_python_refuses_one_byte_over(self) -> None:
        oversized = b"x" * (reg.MAX_BLOB_BYTES + 1)
        with pytest.raises(reg.ExtensionTooLargeError, match="over the"):
            reg.check_size(oversized, name="huge")

    def test_python_accepts_exactly_the_cap(self) -> None:
        """The boundary is inclusive on both sides of the fence — otherwise the
        two enforcement points can disagree by one byte and nobody notices."""
        reg.check_size(b"x" * reg.MAX_BLOB_BYTES, name="exactly-at-cap")

    def test_the_error_names_both_numbers(self) -> None:
        """An operator's next action is deleting files, which needs the actual
        size and the ceiling, not "too large"."""
        with pytest.raises(reg.ExtensionTooLargeError) as exc:
            reg.check_size(b"x" * (reg.MAX_BLOB_BYTES + 1), name="huge")
        assert str(reg.MAX_BLOB_BYTES) in str(exc.value)
        assert str(reg.MAX_BLOB_BYTES + 1) in str(exc.value)

    def test_the_database_refuses_one_byte_over_too(self, db_conn: psycopg.Connection) -> None:
        """The DB carries the SAME number. A writer that skips `check_size`
        still cannot land an oversized blob — which is what makes the cap a
        property of the cluster rather than of one code path."""
        oversized = b"x" * (reg.MAX_BLOB_BYTES + 1)
        with db_conn.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                "INSERT INTO extension_blobs (content_hash, archive, size_bytes) "
                "VALUES (%s, %s, %s)",
                ("over-cap", oversized, len(oversized)),
            )

    def test_the_database_accepts_exactly_the_cap(self, db_conn: psycopg.Connection) -> None:
        """Pins the two numbers EQUAL rather than merely both-present: with this
        and the test above, the DB's ceiling is provably `MAX_BLOB_BYTES`."""
        at_cap = b"x" * reg.MAX_BLOB_BYTES
        with db_conn.transaction(force_rollback=True):
            db_conn.execute(
                "INSERT INTO extension_blobs (content_hash, archive, size_bytes) "
                "VALUES (%s, %s, %s)",
                ("at-cap", at_cap, len(at_cap)),
            )

    def test_a_lying_size_is_refused(self, db_conn: psycopg.Connection) -> None:
        """Without this, both caps check a number the writer chose rather than
        the bytes stored — a 1-byte `size_bytes` would carry any archive."""
        with db_conn.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                "INSERT INTO extension_blobs (content_hash, archive, size_bytes) "
                "VALUES (%s, %s, %s)",
                ("liar", b"x" * 100, 1),
            )

    def test_an_empty_archive_is_refused(self, db_conn: psycopg.Connection) -> None:
        """Zero bytes is not a package; a row pointing at nothing would
        materialize an empty tree and read as success."""
        with db_conn.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                "INSERT INTO extension_blobs (content_hash, archive, size_bytes) "
                "VALUES (%s, %s, %s)",
                ("empty", b"", 0),
            )


class TestContentAddressing:
    def test_packing_is_deterministic(self, tmp_path: Path) -> None:
        """Byte-identical for identical content — the property the whole
        content-addressing scheme rests on. A tar embedding mtimes would store
        different bytes under the same hash on two machines."""
        a = _tree(tmp_path / "a", {"SKILL.md": "hello", "ref/x.md": "world"})
        b = _tree(tmp_path / "b", {"SKILL.md": "hello", "ref/x.md": "world"})
        assert reg.pack_tree(a) == reg.pack_tree(b)

    def test_different_content_packs_differently(self, tmp_path: Path) -> None:
        a = _tree(tmp_path / "a", {"SKILL.md": "hello"})
        b = _tree(tmp_path / "b", {"SKILL.md": "goodbye"})
        assert reg.pack_tree(a) != reg.pack_tree(b)

    def test_ignored_names_are_excluded(self, tmp_path: Path) -> None:
        """`__pycache__` / `.git` must not change the hash, or every machine
        that ever imported the package computes a different identity."""
        clean = _tree(tmp_path / "clean", {"SKILL.md": "hello"})
        dirty = _tree(
            tmp_path / "dirty",
            {"SKILL.md": "hello", "__pycache__/x.pyc": "junk", ".git/config": "junk"},
        )
        assert reg.pack_tree(clean) == reg.pack_tree(dirty)

    def test_round_trip_restores_the_tree(self, tmp_path: Path) -> None:
        src = _tree(tmp_path / "src", {"SKILL.md": "hello", "ref/deep/x.md": "world"})
        reg.unpack_tree(reg.pack_tree(src), tmp_path / "dest")
        assert (tmp_path / "dest" / "SKILL.md").read_text() == "hello"
        assert (tmp_path / "dest" / "ref/deep/x.md").read_text() == "world"

    def test_an_escaping_entry_is_refused(self, tmp_path: Path) -> None:
        """Blob content arrives from outside the cluster. A `../` entry would
        write outside the extension's own directory."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="../escaped.md")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))
        with pytest.raises(ValueError, match="escapes the destination"):
            reg.unpack_tree(buf.getvalue(), tmp_path / "dest")
        assert not (tmp_path / "escaped.md").exists()


class TestRowsAndBlobs:
    def test_register_tree_writes_row_and_blob(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        src = _tree(tmp_path / "pkg", {"SKILL.md": "body"})
        archive = reg.pack_tree(src)
        with db_conn.transaction(force_rollback=True):
            digest = reg.put_blob(db_conn, archive, name="pkg")
            reg.upsert(
                db_conn,
                name="pkg",
                kind="skill",
                source="https://example.invalid/pkg",
                content_hash=digest,
            )
            row = reg.get(db_conn, "pkg")
            assert row is not None
            assert row.kind == "skill"
            assert row.content_hash == digest
            assert row.default_enabled is True
            assert reg.get_blob(db_conn, digest) == archive

    def test_storing_the_same_blob_twice_is_a_noop(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """Blobs are immutable and content-addressed; reinstalling unchanged
        content is the common case and must not rewrite anything."""
        archive = reg.pack_tree(_tree(tmp_path / "pkg", {"SKILL.md": "body"}))
        with db_conn.transaction(force_rollback=True):
            first = reg.put_blob(db_conn, archive, name="pkg")
            second = reg.put_blob(db_conn, archive, name="pkg")
            assert first == second
            count = db_conn.execute(
                "SELECT count(*) FROM extension_blobs WHERE content_hash = %s", (first,)
            ).fetchone()
            assert count is not None
            assert count[0] == 1

    def test_a_row_cannot_point_at_a_missing_blob(self, db_conn: psycopg.Connection) -> None:
        """The FK is what makes "materialize this row" always answerable."""
        with (
            db_conn.transaction(force_rollback=True),
            pytest.raises(psycopg.errors.ForeignKeyViolation),
        ):
            db_conn.execute(
                "INSERT INTO extensions (name, kind, source, content_hash) VALUES (%s, %s, %s, %s)",
                ("ghost", "skill", "https://example.invalid/x", "no-such-hash"),
            )


class TestRepoRowsCarryNoContent:
    """ "The registry owns what arrives by install, not by release" — as a
    schema fact. Repo content is already cluster-consistent via commit-pinned
    rollout, so a repo row exists only to carry `default_enabled`."""

    def test_a_repo_row_with_a_blob_is_refused(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        archive = reg.pack_tree(_tree(tmp_path / "pkg", {"SKILL.md": "body"}))
        with db_conn.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
            digest = reg.put_blob(db_conn, archive, name="pkg")
            reg.upsert(
                db_conn,
                name="repo-pkg",
                kind="skill",
                source=reg.REPO_SOURCE,
                content_hash=digest,
            )

    def test_an_installed_row_without_a_blob_is_refused(self, db_conn: psycopg.Connection) -> None:
        """The other half of the iff: content that arrived by install must be
        stored, or the row promises a materialization it cannot deliver."""
        with db_conn.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
            reg.upsert(
                db_conn,
                name="hollow",
                kind="skill",
                source="https://example.invalid/x",
                content_hash=None,
            )

    def test_a_repo_row_without_a_blob_is_accepted(self, db_conn: psycopg.Connection) -> None:
        with db_conn.transaction(force_rollback=True):
            reg.upsert(
                db_conn, name="repo-pkg", kind="skill", source=reg.REPO_SOURCE, content_hash=None
            )
            row = reg.get(db_conn, "repo-pkg")
            assert row is not None
            assert row.is_repo_source
            assert row.content_hash is None


class TestEnablementIsClusterPolicy:
    def test_reinstall_preserves_a_disabled_row(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """The load-bearing one. `default_enabled` is an operator's decision;
        upgrading a package must not silently switch it back on."""
        archive = reg.pack_tree(_tree(tmp_path / "pkg", {"SKILL.md": "v1"}))
        with db_conn.transaction(force_rollback=True):
            digest = reg.put_blob(db_conn, archive, name="pkg")
            reg.upsert(
                db_conn, name="pkg", kind="skill", source="https://x.invalid", content_hash=digest
            )
            assert reg.set_default_enabled(db_conn, "pkg", enabled=False) is True

            newer = reg.pack_tree(_tree(tmp_path / "pkg2", {"SKILL.md": "v2"}))
            new_digest = reg.put_blob(db_conn, newer, name="pkg")
            reg.upsert(
                db_conn,
                name="pkg",
                kind="skill",
                source="https://x.invalid",
                content_hash=new_digest,
                version="2.0",
            )

            row = reg.get(db_conn, "pkg")
            assert row is not None
            assert row.content_hash == new_digest, "the upgrade must land"
            assert row.default_enabled is False, "but must not re-enable a disabled extension"

    def test_list_enabled_narrows_by_kind_and_skips_disabled(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        archive = reg.pack_tree(_tree(tmp_path / "pkg", {"SKILL.md": "body"}))
        with db_conn.transaction(force_rollback=True):
            digest = reg.put_blob(db_conn, archive, name="pkg")
            for name, kind in (("s-on", "skill"), ("s-off", "skill"), ("p-on", "plugin")):
                reg.upsert(
                    db_conn,
                    name=name,
                    kind=kind,  # pyright: ignore[reportArgumentType]
                    source="https://x.invalid",
                    content_hash=digest,
                )
            reg.set_default_enabled(db_conn, "s-off", enabled=False)

            names = [e.name for e in reg.list_enabled(db_conn, kind="skill")]
            assert names == ["s-on"]

    def test_set_default_enabled_reports_an_unknown_name(self, db_conn: psycopg.Connection) -> None:
        """False rather than a silent success — the caller's `ava skill enable
        <typo>` has to be able to say so."""
        with db_conn.transaction(force_rollback=True):
            assert reg.set_default_enabled(db_conn, "no-such-extension", enabled=True) is False
