"""Locality rules coverage: package doors (Rule 4) and single decision owners (Rule 5)."""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess

import pytest

from scripts import lint_code_structure as lcs
from scripts.structure import locality


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _baseline(
    root: pathlib.Path,
    *,
    files: dict[str, int] | None = None,
    directories: dict[str, int] | None = None,
    complexity: dict[str, int] | None = None,
    nesting: dict[str, int] | None = None,
    private_imports: dict[str, int] | None = None,
    owner_bypasses: dict[str, int] | None = None,
) -> pathlib.Path:
    path = root / "scripts/structure/baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "directories": directories or {},
                "files": files or {},
                "complexity": complexity or {},
                "nesting": nesting or {},
                "private_imports": private_imports or {},
                "owner_bypasses": owner_bypasses or {},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — arguments are fixed test commands, never external input.
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def _repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A repo root wired to lcs._REPO_ROOT, with an all-empty baseline already in place."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _baseline(tmp_path)
    return tmp_path


# --- private_imports: package doors -----------------------------------------


def test_reach_in_from_outside_owner_is_flagged(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/_priv/mod.py", "y = 1\n")
    tree = _parse("import a._priv.mod\nimport a._priv.mod\n")

    sites = locality.private_imports(tree, "b/importer.py", ("a", "b"), tmp_path)

    assert sites == {"b/importer.py::a._priv": [1, 2]}


def test_reach_in_from_owner_itself_is_not_flagged(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/_priv/mod.py", "y = 1\n")
    tree = _parse("import a._priv.mod\n")

    assert locality.private_imports(tree, "a/other.py", ("a", "b"), tmp_path) == {}


def test_reach_in_from_a_subpackage_of_the_owner_is_not_flagged(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/_priv/mod.py", "y = 1\n")
    _write(tmp_path, "a/sub/deep.py", "z = 1\n")
    tree = _parse("import a._priv.mod\n")

    assert locality.private_imports(tree, "a/sub/deep.py", ("a", "b"), tmp_path) == {}


def test_module_level_private_name_owner_is_the_modules_package(tmp_path: pathlib.Path) -> None:
    """`a/b.py` is a plain module file (no `a/b/` directory), so the private name
    `_fn` it defines is package-private to `a`, not to a nonexistent package `a.b`."""
    _write(tmp_path, "a/b.py", "def _fn(): ...\n")
    tree = _parse("from a.b import _fn\n")

    assert locality.private_imports(tree, "a/other.py", ("a", "c"), tmp_path) == {}
    assert locality.private_imports(tree, "c/importer.py", ("a", "c"), tmp_path) == {
        "c/importer.py::a.b._fn": [1]
    }


def test_relative_import_resolved_against_importer_stays_inside_owner(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "a/_x.py", "y = 1\n")
    tree = _parse("from .._x import y\n")

    sites = locality.private_imports(tree, "a/sub/importer.py", ("a",), tmp_path)

    assert sites == {}


def test_relative_import_flagged_outside_a_nested_owner(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/d/_e.py", "f = 1\n")
    tree = _parse("from ..d._e import f\n")

    sites = locality.private_imports(tree, "a/sub/importer.py", ("a",), tmp_path)

    assert sites == {"a/sub/importer.py::a.d._e": [1]}


def test_dunder_names_are_never_private(tmp_path: pathlib.Path) -> None:
    tree = _parse("from a import __version__\n")

    assert locality.private_imports(tree, "b/importer.py", ("a", "b"), tmp_path) == {}


def test_ungoverned_roots_are_ignored(tmp_path: pathlib.Path) -> None:
    tree = _parse("from psycopg._x import y\n")

    assert locality.private_imports(tree, "a/importer.py", ("a",), tmp_path) == {}


def test_multiple_private_names_from_one_module_count_as_one_site(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "a/_b.py", "c = 1\nd = 1\n")
    tree = _parse("from a._b import c, d\n")

    sites = locality.private_imports(tree, "e/importer.py", ("a", "e"), tmp_path)

    assert sites == {"e/importer.py::a._b": [1]}


# --- owner_bypasses: the postgres-dial single decision owner ----------------

_POSITIVE_DIALS = {
    "module_connect": "import psycopg\npsycopg.connect('dsn')\n",
    "aliased_module_connect": "import psycopg as pg\npg.connect('dsn')\n",
    "from_connection_connect": "from psycopg import Connection\nConnection.connect('dsn')\n",
    "from_async_connection_connect": (
        "from psycopg import AsyncConnection\nAsyncConnection.connect('dsn')\n"
    ),
    "qualified_async_connection_connect": (
        "import psycopg\npsycopg.AsyncConnection.connect('dsn')\n"
    ),
    "connection_pool": "from psycopg_pool import ConnectionPool\nConnectionPool('dsn')\n",
    "subscripted_async_connection_pool": (
        "from psycopg_pool import AsyncConnectionPool\nAsyncConnectionPool[int]('dsn')\n"
    ),
}


@pytest.mark.parametrize("source", _POSITIVE_DIALS.values(), ids=_POSITIVE_DIALS.keys())
def test_postgres_dial_forms_are_flagged(source: str) -> None:
    sites = locality.owner_bypasses(_parse(source), "gateway/db.py")

    assert sites == {"gateway/db.py::postgres-dial": [2]}


_NEGATIVE_DIALS = {
    "sqlite3_connect": "import sqlite3\nsqlite3.connect('file.db')\n",
    "connection_from_a_non_psycopg_module": (
        "from somewhere import Connection\nConnection.connect()\n"
    ),
    "pool_method_call": (
        "from psycopg_pool import ConnectionPool\nConnectionPool.check_connection('x')\n"
    ),
    "bare_annotation": "from psycopg import Connection\ndef f(conn: Connection) -> None: ...\n",
}


@pytest.mark.parametrize("source", _NEGATIVE_DIALS.values(), ids=_NEGATIVE_DIALS.keys())
def test_non_dial_calls_are_not_flagged(source: str) -> None:
    assert locality.owner_bypasses(_parse(source), "gateway/db.py") == {}


def test_the_owner_module_itself_is_exempt() -> None:
    tree = _parse("import psycopg\npsycopg.connect('dsn')\n")

    assert locality.owner_bypasses(tree, "shared/db_connections.py") == {}


# --- measure: test files are exempt from both rules --------------------------

_REACHES_AND_DIALS = "import a._priv.mod\nimport psycopg\npsycopg.connect('dsn')\n"


@pytest.mark.parametrize(
    "rel_path",
    ["tests/gateway/db.py", "gateway/test_db.py", "gateway/db_test.py", "tests/db.py"],
)
def test_measure_exempts_test_files(tmp_path: pathlib.Path, rel_path: str) -> None:
    _write(tmp_path, "a/_priv/mod.py", "x = 1\n")

    measured = locality.measure(_parse(_REACHES_AND_DIALS), rel_path, ("a",), tmp_path)

    assert measured == {"private_imports": {}, "owner_bypasses": {}}


def test_measure_scans_non_test_files(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/_priv/mod.py", "x = 1\n")

    measured = locality.measure(_parse(_REACHES_AND_DIALS), "gateway/db.py", ("a",), tmp_path)

    assert measured["private_imports"] == {"gateway/db.py::a._priv": [1]}
    assert measured["owner_bypasses"] == {"gateway/db.py::postgres-dial": [3]}


# --- site_errors: frozen counts must match reality exactly ------------------


def _site_errors(
    tmp_path: pathlib.Path,
    *,
    measured: dict[str, list[int]],
    frozen: dict[str, int],
    scanned: set[str],
    exists: frozenset[str] = frozenset(),
    renames: dict[str, str] | None = None,
) -> list[str]:
    for rel in exists:
        _write(tmp_path, rel, "x = 1\n")
    return locality.site_errors(
        {"private_imports": measured, "owner_bypasses": {}},
        {"private_imports": frozen, "owner_bypasses": {}},
        scanned=scanned,
        repo_root=tmp_path,
        renames=renames,
    )


def test_site_errors_flags_a_brand_new_site(tmp_path: pathlib.Path) -> None:
    errors = _site_errors(
        tmp_path, measured={"gateway/db.py::a._priv": [5]}, frozen={}, scanned={"gateway/db.py"}
    )

    assert len(errors) == 1
    assert errors[0].startswith("gateway/db.py:5: imports private `a._priv`")
    assert "grew above" not in errors[0]
    assert "renamed file" not in errors[0]


def test_site_errors_flags_growth_above_the_frozen_count(tmp_path: pathlib.Path) -> None:
    errors = _site_errors(
        tmp_path,
        measured={"gateway/db.py::a._priv": [5, 9]},
        frozen={"gateway/db.py::a._priv": 1},
        scanned={"gateway/db.py"},
    )

    assert len(errors) == 2
    assert errors[0].startswith("gateway/db.py:5:")
    assert errors[1].startswith("gateway/db.py:9:")
    assert all("grew above its frozen count 1" in error for error in errors)


def test_site_errors_flags_shrinkage_to_lower(tmp_path: pathlib.Path) -> None:
    errors = _site_errors(
        tmp_path,
        measured={"gateway/db.py::a._priv": [5]},
        frozen={"gateway/db.py::a._priv": 3},
        scanned={"gateway/db.py"},
    )

    assert len(errors) == 1
    assert "stale private_imports entry gateway/db.py::a._priv" in errors[0]
    assert "frozen at 3 but the code has 1" in errors[0]
    assert "lower it to 1" in errors[0]


def test_site_errors_flags_a_removed_site_to_remove(tmp_path: pathlib.Path) -> None:
    errors = _site_errors(
        tmp_path, measured={}, frozen={"gateway/db.py::a._priv": 2}, scanned={"gateway/db.py"}
    )

    assert len(errors) == 1
    assert "stale private_imports entry gateway/db.py::a._priv" in errors[0]
    assert "the code has 0" in errors[0]
    assert "remove it" in errors[0]


def test_site_errors_flags_a_deleted_files_entry_as_stale(tmp_path: pathlib.Path) -> None:
    """A frozen entry for a file that no longer exists is checked even if this
    run never scanned it — it cannot hide behind "not scanned this time"."""
    errors = _site_errors(
        tmp_path, measured={}, frozen={"gateway/gone.py::a._priv": 1}, scanned=set()
    )

    assert len(errors) == 1
    assert "stale private_imports entry gateway/gone.py::a._priv" in errors[0]
    assert "remove it" in errors[0]


def test_site_errors_skips_an_unscanned_but_still_existing_file(tmp_path: pathlib.Path) -> None:
    errors = _site_errors(
        tmp_path,
        measured={},
        frozen={"gateway/other.py::a._priv": 1},
        scanned=set(),
        exists=frozenset({"gateway/other.py"}),
    )

    assert errors == []


def test_site_errors_new_site_on_a_renamed_file_gets_a_migration_hint(
    tmp_path: pathlib.Path,
) -> None:
    errors = _site_errors(
        tmp_path,
        measured={"gateway/db_new.py::a._priv": [5]},
        frozen={},
        scanned={"gateway/db_new.py"},
        renames={"gateway/db_old.py": "gateway/db_new.py"},
    )

    assert len(errors) == 1
    assert "renamed file: migrate the baseline key from gateway/db_old.py" in errors[0]
    assert "grew above" not in errors[0]


# --- allowlist_errors: a listed exemption must still bypass the owner -------


def test_allowlist_entry_is_stale_once_the_module_stops_bypassing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = locality.Decision(
        owners=frozenset({"shared/owner.py"}),
        find=lambda _tree: [],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    errors = locality.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py")

    assert len(errors) == 1
    lineno, message = errors[0]
    assert lineno == 1
    assert "stale fake-decision allowlist entry" in message
    assert "remove it from DECISIONS" in message


def test_allowlist_entry_is_not_stale_while_it_still_bypasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = locality.Decision(
        owners=frozenset({"shared/owner.py"}),
        find=lambda _tree: [3],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    assert locality.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py") == []


def test_allowlist_errors_ignore_modules_outside_the_allowed_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = locality.Decision(
        owners=frozenset(), find=lambda _tree: [], fix="use the owner", allowed={}
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    assert locality.allowlist_errors(_parse("x = 1\n"), "gateway/other.py") == []


# --- validate_entries: baseline schema rejections ----------------------------


def test_validate_entries_rejects_a_non_dict_section() -> None:
    with pytest.raises(ValueError, match="'private_imports' must be an object"):
        locality.validate_entries("private_imports", [], ("a",))


@pytest.mark.parametrize(
    "entry",
    [
        "a/mod.py",  # missing "::"
        "a/mod.py::",  # empty target
        "/a/mod.py::x",  # absolute path
        "a/../mod.py::x",  # ".." escapes scope
        "c/mod.py::x",  # out of scope
        "a/mod.txt::x",  # non-.py suffix
    ],
)
def test_validate_private_imports_rejects_malformed_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="invalid private_imports entry"):
        locality.validate_entries("private_imports", {entry: 1}, ("a",))


@pytest.mark.parametrize("count", [0, -1, True, "1", 1.0, None])
def test_validate_private_imports_rejects_bad_counts(count: object) -> None:
    with pytest.raises(ValueError, match="invalid private_imports entry"):
        locality.validate_entries("private_imports", {"a/mod.py::x": count}, ("a",))


def test_validate_private_imports_accepts_a_well_formed_entry() -> None:
    locality.validate_entries("private_imports", {"a/mod.py::b._x": 1}, ("a",))


@pytest.mark.parametrize(
    "entry",
    [
        "a/mod.py",  # missing "::"
        "a/mod.py::",  # empty target
        "a/mod.py::not-a-decision",  # unknown decision name
        "/a/mod.py::postgres-dial",  # absolute path
        "c/mod.py::postgres-dial",  # out of scope
    ],
)
def test_validate_owner_bypasses_rejects_malformed_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="invalid owner_bypasses entry"):
        locality.validate_entries("owner_bypasses", {entry: 1}, ("a",))


def test_validate_owner_bypasses_accepts_a_known_decision_name() -> None:
    # Exercises the real registry: "postgres-dial" is the one decision it names today.
    locality.validate_entries("owner_bypasses", {"a/mod.py::postgres-dial": 1}, ("a",))


# --- end-to-end through lcs.main ---------------------------------------------


def test_a_new_reach_in_fails_the_gate(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "gateway/importer.py:1:" in output
    assert "imports private `shared._priv`" in output


def test_the_same_reach_in_frozen_in_the_baseline_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_removing_the_reach_in_but_keeping_the_entry_fails_as_stale(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "value = 1\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "stale private_imports entry gateway/importer.py::shared._priv" in output
    assert "remove it" in output


def test_baseline_guard_rejects_an_unpaired_new_private_imports_key(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(_repo, "init", "--quiet")
    _git(_repo, "add", "scripts/structure/baseline.json")
    _git(_repo, "commit", "--quiet", "-m", "Freeze empty baseline")

    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "added private_imports entry gateway/importer.py::shared._priv" in output
    assert "added key without a paired same-file removal" in output


def test_baseline_guard_accepts_a_same_file_pairing(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _baseline(_repo, private_imports={"gateway/importer.py::shared._old": 1})
    _git(_repo, "init", "--quiet")
    _git(_repo, "add", "scripts/structure/baseline.json")
    _git(_repo, "commit", "--quiet", "-m", "Freeze the old reach-in")

    _write(_repo, "shared/_new/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._new import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._new": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""
