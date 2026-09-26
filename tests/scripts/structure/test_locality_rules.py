"""Unit coverage for scripts/structure/locality.py: package doors (Rule 4) and
single decision owners (Rule 5), exercised directly against the module's
functions (no lcs.main, no git — see test_locality_gate.py for that)."""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import locality


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- private_imports: basic owner resolution ---------------------------------


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


def test_module_file_owner_wins_over_a_same_named_docs_directory(tmp_path: pathlib.Path) -> None:
    """A leftover OKF docs folder or __pycache__ beside a module must not flip
    the owner from "module" to "package": Python-style resolution (`<prefix>.py`
    is a file) wins over the coincidental directory of the same name."""
    _write(tmp_path, "a/b.py", "def _fn(): ...\n")
    _write(tmp_path, "a/b/b.ava.okf.md", "# docs\n")
    tree = _parse("from a.b import _fn\n")

    assert locality.private_imports(tree, "a/c.py", ("a",), tmp_path) == {}
    assert locality.private_imports(tree, "z/y.py", ("a", "z"), tmp_path) == {
        "z/y.py::a.b._fn": [1]
    }


def test_dot_boundary_a_sibling_package_prefix_match_is_still_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """Owner `a.b` must not match importer `a.bc` on a raw string prefix —
    `a.bc` is a sibling of `a.b`, not one of its subpackages."""
    _write(tmp_path, "a/b/_x.py", "y = 1\n")
    tree = _parse("from a.b import _x\n")

    sites = locality.private_imports(tree, "a/bc/m.py", ("a",), tmp_path)

    assert sites == {"a/bc/m.py::a.b._x": [1]}


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


# --- private_imports: attribute reach-ins on an imported module alias -------


def _shared_lm_and_db(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "shared/lm/_effort.py", "x = 1\n")
    _write(tmp_path, "shared/db.py", "class Foo:\n    _x = 1\n\n\ndef _restore(): ...\n")


def test_attribute_reach_in_via_import_module(tmp_path: pathlib.Path) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("import shared.lm\nshared.lm._effort.x\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path)

    assert sites == {"gateway/x.py::shared.lm._effort": [2]}


def test_attribute_reach_in_via_from_import_package(tmp_path: pathlib.Path) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("from shared import lm\nlm._effort\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path)

    assert sites == {"gateway/x.py::shared.lm._effort": [2]}


def test_attribute_reach_in_via_from_import_module(tmp_path: pathlib.Path) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("from shared import db\ndb._restore()\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path)

    assert sites == {"gateway/x.py::shared.db._restore": [2]}


def test_attribute_on_an_imported_class_is_not_a_module_reach_in(tmp_path: pathlib.Path) -> None:
    """`Foo` is bound to a class, not a module or package on disk, so its
    private attribute is out of Rule 4's reach."""
    _shared_lm_and_db(tmp_path)
    tree = _parse("from shared.db import Foo\nFoo._x\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path) == {}


def test_attribute_chain_past_a_class_stops_at_the_module_boundary(
    tmp_path: pathlib.Path,
) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("from shared import db\ndb.Foo._x\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path) == {}


def test_an_unbound_local_attribute_is_never_a_reach_in(tmp_path: pathlib.Path) -> None:
    tree = _parse("obj._x\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path) == {}


def test_attribute_reach_in_from_inside_the_owner_is_not_flagged(tmp_path: pathlib.Path) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("import shared.lm\nshared.lm._effort.x\n")

    assert locality.private_imports(tree, "shared/lm/other.py", ("shared",), tmp_path) == {}


def test_a_long_attribute_chain_counts_the_site_once(tmp_path: pathlib.Path) -> None:
    _shared_lm_and_db(tmp_path)
    tree = _parse("import shared.lm\nshared.lm._effort.a.b\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("shared",), tmp_path)

    assert sites == {"gateway/x.py::shared.lm._effort": [2]}


# --- private_imports: FRAMEWORK_TIERS ----------------------------------------


def test_framework_tier_import_from_is_not_flagged(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "ava/_boot.py", "x = 1\n")
    tree = _parse("from ava import _boot\n")

    assert locality.private_imports(tree, "gateway/x.py", ("ava", "gateway"), tmp_path) == {}


def test_framework_tier_attribute_reach_in_is_not_flagged(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "ava/_boot.py", "def f(): ...\n")
    tree = _parse("import ava\nava._boot.f()\n")

    assert locality.private_imports(tree, "gateway/x.py", ("ava", "gateway"), tmp_path) == {}


def test_a_nested_package_under_the_tier_is_not_itself_a_tier(tmp_path: pathlib.Path) -> None:
    """`ava.shell` is not a FRAMEWORK_TIERS entry (only the bare `ava` root is);
    a private submodule owned by `ava.shell` is a normal package door."""
    _write(tmp_path, "ava/shell/_x.py", "y = 1\n")
    tree = _parse("from ava.shell import _x\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("ava", "gateway"), tmp_path)

    assert sites == {"gateway/x.py::ava.shell._x": [1]}


# --- owner_bypasses: the postgres-dial single decision owner ----------------

_POSITIVE_DIALS = {
    "module_connect": "import psycopg\npsycopg.connect('dsn')\n",
    "aliased_module_connect": "import psycopg as pg\npg.connect('dsn')\n",
    "from_connect_function": "from psycopg import connect\nconnect()\n",
    "aliased_connect_function": "from psycopg import connect as c\nc()\n",
    "from_async_connection_connect": (
        "from psycopg import AsyncConnection\nAsyncConnection.connect()\n"
    ),
    "qualified_async_connection_connect": "import psycopg\npsycopg.AsyncConnection.connect()\n",
    "connection_pool": "from psycopg_pool import ConnectionPool\nConnectionPool()\n",
    "subscripted_aliased_async_connection_pool": (
        "from psycopg_pool import AsyncConnectionPool as P\nP[int]()\n"
    ),
    "qualified_pool_module_connection_pool": (
        "import psycopg_pool\npsycopg_pool.ConnectionPool()\n"
    ),
}


@pytest.mark.parametrize("source", _POSITIVE_DIALS.values(), ids=_POSITIVE_DIALS.keys())
def test_postgres_dial_forms_are_flagged(source: str) -> None:
    sites = locality.owner_bypasses(_parse(source), "gateway/db.py", ())

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_governed_subclass_construction_is_a_dial() -> None:
    """This repo's own pool subclasses (e.g. agent/db.py's LoggingConnectionPool)
    are governed reach-ins too, not just the psycopg_pool names themselves."""
    source = "from agent.db import LoggingConnectionPool\nLoggingConnectionPool[int]('dsn')\n"

    sites = locality.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"))

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_ungoverned_subclass_construction_is_not_a_dial() -> None:
    source = "from somepkg.db import LoggingConnectionPool\nLoggingConnectionPool()\n"

    sites = locality.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"))

    assert sites == {}


def test_a_locally_defined_pool_subclass_is_a_dial() -> None:
    source = (
        "from psycopg_pool import AsyncConnectionPool\n"
        "class P(AsyncConnectionPool[int]):\n"
        "    pass\n\n\n"
        "P()\n"
    )

    sites = locality.owner_bypasses(_parse(source), "gateway/db.py", ())

    assert sites == {"gateway/db.py::postgres-dial": [6]}


_NEGATIVE_DIALS = {
    "sqlite3_connect": "import sqlite3\nsqlite3.connect('file.db')\n",
    "connection_from_a_non_psycopg_module": (
        "from mylib import Connection\nConnection.connect()\n"
    ),
    "pool_method_call": (
        "from psycopg_pool import ConnectionPool\nConnectionPool.check_connection('x')\n"
    ),
    "unbound_redis_attribute_pool": "import redis\nredis.ConnectionPool()\n",
    "ungoverned_third_party_pool": "from redis import ConnectionPool\nConnectionPool()\n",
    "unbound_urllib3_https_pool": "import urllib3\nurllib3.HTTPSConnectionPool('h')\n",
    "pool_timeout_from_import": "from psycopg_pool import PoolTimeout\nPoolTimeout()\n",
    "pool_timeout_qualified": "import psycopg_pool\npsycopg_pool.PoolTimeout()\n",
    "bare_annotation": "from psycopg import Connection\ndef f(conn: Connection) -> None: ...\n",
}


@pytest.mark.parametrize("source", _NEGATIVE_DIALS.values(), ids=_NEGATIVE_DIALS.keys())
def test_non_dial_calls_are_not_flagged(source: str) -> None:
    assert locality.owner_bypasses(_parse(source), "gateway/db.py", ()) == {}


def test_the_owner_module_itself_is_exempt() -> None:
    tree = _parse("import psycopg\npsycopg.connect('dsn')\n")

    assert locality.owner_bypasses(tree, "shared/db_connections.py", ()) == {}


# --- measure: test *directories* are exempt, test-prefixed files are not ----


def test_test_directory_is_exempt_but_a_test_prefixed_governed_file_is_scanned(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "a/_priv/mod.py", "x = 1\n")
    tree = _parse("import a._priv.mod\n")

    assert locality.measure(tree, "gateway/tests/x.py", ("a",), tmp_path) == {
        "private_imports": {},
        "owner_bypasses": {},
    }
    measured = locality.measure(tree, "gateway/test_db.py", ("a",), tmp_path)
    assert measured["private_imports"] == {"gateway/test_db.py::a._priv": [1]}


def test_measure_scans_a_non_test_file_for_both_rules(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "a/_priv/mod.py", "x = 1\n")
    source = "import a._priv.mod\nimport psycopg\npsycopg.connect('dsn')\n"

    measured = locality.measure(_parse(source), "gateway/db.py", ("a",), tmp_path)

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
    assert errors[0].startswith("gateway/db.py:5: reaches private `a._priv`")
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


# --- allowlist_errors / missing_allowlist_errors -----------------------------


def test_allowlist_entry_is_stale_once_the_module_stops_bypassing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = locality.Decision(
        owners=frozenset({"shared/owner.py"}),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    errors = locality.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py", ())

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
        find=lambda _tree, _roots: [3],
        fix="use the owner",
        allowed={"gateway/legacy.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    assert locality.allowlist_errors(_parse("x = 1\n"), "gateway/legacy.py", ()) == []


def test_allowlist_errors_ignore_modules_outside_the_allowed_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = locality.Decision(
        owners=frozenset(), find=lambda _tree, _roots: [], fix="use the owner", allowed={}
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    assert locality.allowlist_errors(_parse("x = 1\n"), "gateway/other.py", ()) == []


def test_missing_allowlist_errors_flags_a_deleted_allowed_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = locality.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/gone.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    errors = locality.missing_allowlist_errors(tmp_path)

    assert len(errors) == 1
    assert "gateway/gone.py:1: stale fake-decision allowlist entry" in errors[0]
    assert "the module no longer exists" in errors[0]


def test_missing_allowlist_errors_is_clean_for_an_existing_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "gateway/present.py", "x = 1\n")
    decision = locality.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        allowed={"gateway/present.py": "historical exemption"},
    )
    monkeypatch.setattr(locality, "DECISIONS", {"fake-decision": decision})

    assert locality.missing_allowlist_errors(tmp_path) == []


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


# --- unpaired_additions: leaf-name pairing for the base-revision guard ------


def test_unpaired_additions_same_file_same_leaf_pairs() -> None:
    previous = {"gateway/db.py::shared._old": 2}
    current = {"gateway/db.py::shared.sub._old": 2}

    assert locality.unpaired_additions(current, previous) == []


def test_unpaired_additions_same_file_different_leaf_does_not_pair() -> None:
    previous = {"gateway/db.py::shared._old": 2}
    current = {"gateway/db.py::shared._new": 2}

    assert locality.unpaired_additions(current, previous) == ["gateway/db.py::shared._new"]


def test_unpaired_additions_different_file_does_not_pair() -> None:
    previous = {"gateway/db.py::shared._old": 2}
    current = {"other/db.py::shared._old": 2}

    assert locality.unpaired_additions(current, previous) == ["other/db.py::shared._old"]


def test_unpaired_additions_value_above_the_removed_one_does_not_pair() -> None:
    previous = {"gateway/db.py::shared._old": 1}
    current = {"gateway/db.py::shared.sub._old": 2}

    assert locality.unpaired_additions(current, previous) == ["gateway/db.py::shared.sub._old"]
