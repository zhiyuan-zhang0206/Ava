"""Unit coverage for scripts/structure/locality.py: package doors (Rule 4) and
single decision owners (Rule 5), exercised directly against the module's
functions (no lcs.main, no git — see test_locality_gate.py for that)."""

from __future__ import annotations

import ast
import os
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


# --- private_imports: `ava` has no exemption ---------------------------------
# Agent visibility in `ava` is the `__all_for_ava__` whitelist, not the
# underscore, so `ava/_*.py` is package-private like any other package.


@pytest.mark.parametrize(
    ("files", "source", "key"),
    [
        ({"ava/_boot.py": "x = 1\n"}, "from ava import _boot\n", "ava._boot"),
        ({"ava/_boot.py": "def f(): ...\n"}, "import ava\nava._boot.f()\n", "ava._boot"),
        ({"ava/_pkg/__init__.py": "x = 1\n"}, "from ava import _pkg\n", "ava._pkg"),
        ({"ava/__init__.py": "def _fn(): ...\n"}, "from ava import _fn\n", "ava._fn"),
        ({"ava/files.py": "x = 1\n"}, "from ava.files import _x\n", "ava.files._x"),
        ({"ava/shell/_x.py": "y = 1\n"}, "from ava.shell import _x\n", "ava.shell._x"),
    ],
    ids=["private-module", "attribute", "private-package", "init-name", "module-name", "nested"],
)
def test_ava_privates_are_flagged_from_outside_ava(
    tmp_path: pathlib.Path, files: dict[str, str], source: str, key: str
) -> None:
    for rel, text in files.items():
        _write(tmp_path, rel, text)

    sites = locality.private_imports(_parse(source), "gateway/x.py", ("ava", "gateway"), tmp_path)

    assert list(sites) == [f"gateway/x.py::{key}"]


def test_ava_privates_are_open_inside_ava(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "ava/_boot.py", "x = 1\n")

    tree = _parse("from ava import _boot\n")

    assert locality.private_imports(tree, "ava/files.py", ("ava",), tmp_path) == {}


# --- private_imports: shadowed aliases (the `rebound` set) ------------------


def test_an_alias_shadowed_by_a_function_parameter_is_not_followed(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "shared/telemetry/__init__.py", "x = 1\n")
    tree = _parse("from shared import telemetry\ndef f(telemetry): return telemetry._state\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared", "gateway"), tmp_path) == {}


def test_an_alias_shadowed_by_reassignment_is_not_followed(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "shared/telemetry/__init__.py", "x = 1\n")
    tree = _parse("from shared import telemetry\ntelemetry = object()\ntelemetry._state\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared", "gateway"), tmp_path) == {}


def test_an_alias_literally_named_self_is_not_followed_inside_a_method(
    tmp_path: pathlib.Path,
) -> None:
    """`self` is an extremely common parameter name; if a module alias happens to
    share it, the scope-free rebound check must still suppress it rather than
    flooding every method with a bogus reach-in."""
    _write(tmp_path, "shared/self.py", "x = 1\n")
    tree = _parse("from shared import self\nclass C:\n    def m(self): return self._x\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared", "gateway"), tmp_path) == {}


def test_an_unshadowed_alias_is_still_flagged(tmp_path: pathlib.Path) -> None:
    """Control case: with no rebinding anywhere in the module, the same alias
    form as the tests above is flagged normally."""
    _write(tmp_path, "shared/telemetry/__init__.py", "x = 1\n")
    tree = _parse("from shared import telemetry\ntelemetry._state\n")

    sites = locality.private_imports(tree, "gateway/x.py", ("shared", "gateway"), tmp_path)

    assert sites == {"gateway/x.py::shared.telemetry._state": [2]}


# --- private_imports: case-exact filesystem checks --------------------------


def test_an_attribute_of_a_module_level_class_is_not_matched_by_case_folding(
    tmp_path: pathlib.Path,
) -> None:
    """`Registry` (capital R) must not resolve as a module just because a
    case-folding filesystem (macOS) would make `Registry.py` and the real
    `registry.py` collide — `_exists_exact` checks the directory listing, not
    just `Path.is_file()`, so this must hold on macOS and Linux alike."""
    _write(tmp_path, "shared/pkg/__init__.py", "x = 1\n")
    _write(tmp_path, "shared/pkg/registry.py", "class Registry: pass\n")
    tree = _parse("from shared import pkg\npkg.Registry._cache.clear()\n")

    assert locality.private_imports(tree, "gateway/x.py", ("shared", "gateway"), tmp_path) == {}


def test_a_wrong_case_module_path_does_not_resolve_to_the_real_module(
    tmp_path: pathlib.Path,
) -> None:
    """`shared.Mod` (capital M) does not exist case-exactly even though
    `shared/mod.py` does — the owner must resolve to the non-existent
    `shared.Mod` "package", not silently fold onto the real module, so the
    importer (which is not inside that non-existent package) is flagged."""
    _write(tmp_path, "shared/mod.py", "x = 1\n")
    tree = _parse("from shared.Mod import _x\n")

    sites = locality.private_imports(tree, "shared/other.py", ("shared",), tmp_path)

    assert sites == {"shared/other.py::shared.Mod._x": [1]}


def test_exists_exact_listing_is_refreshed_when_the_directory_changes(
    tmp_path: pathlib.Path,
) -> None:
    """The cached listing is keyed on the directory's mtime; a new entry in a later
    tick must be seen, and `reset_caches()` covers two writes in one coarse tick."""
    directory = tmp_path / "shared"
    directory.mkdir()
    (directory / "old.py").write_text("x = 1\n", encoding="utf-8")
    assert locality._exists_exact(directory / "old.py", directory=False)  # fills the cache
    tick = directory.stat().st_mtime_ns

    (directory / "fresh.py").write_text("x = 1\n", encoding="utf-8")
    os.utime(directory, ns=(tick, tick))  # same tick: the cached listing is stale
    assert not locality._exists_exact(directory / "fresh.py", directory=False)
    locality.reset_caches()
    assert locality._exists_exact(directory / "fresh.py", directory=False)

    (directory / "later.py").write_text("x = 1\n", encoding="utf-8")
    os.utime(directory, ns=(tick + 10**9, tick + 10**9))  # a later tick
    assert locality._exists_exact(directory / "later.py", directory=False)


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


def test_growth_errors_are_emitted_in_line_order_even_when_recorded_out_of_order(
    tmp_path: pathlib.Path,
) -> None:
    """Attribute-based reach-ins are recorded in a separate pass after every
    import-based one (see private_imports), so a later import line can land in
    `sites` before an earlier attribute line — confirmed below: [5, 2], not
    [2, 5]. site_errors must still report the errors in line order regardless."""
    _write(tmp_path, "a/_priv/mod.py", "x = 1\n")
    source = "import a\na._priv.x\n\n\nimport a._priv\n"

    sites = locality.private_imports(_parse(source), "gateway/x.py", ("a", "gateway"), tmp_path)

    assert sites == {"gateway/x.py::a._priv": [5, 2]}

    errors = _site_errors(tmp_path, measured=sites, frozen={}, scanned={"gateway/x.py"})

    assert len(errors) == 2
    assert errors[0].startswith("gateway/x.py:2:")
    assert errors[1].startswith("gateway/x.py:5:")


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
