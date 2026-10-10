"""The placement rule's dependency step: a home is the deepest package that holds or directly
depends on every module the file references (within the home unit, below the nearest common
ancestor of those modules), and the per-file import cache that feeds it."""

from __future__ import annotations

import ast
import itertools
import json
import os
import pathlib

import pytest

from scripts.structure import imports, locality, placement
from scripts.structure.imports import cache
from scripts.structure.placement_evidence import Placement
from scripts.structure.tests.patch_repo import make_repo, write

RUN = "cli/commands/run.py"
ARGS = "cli/parsers/args.py"
TEST = "from cli.commands import run\nfrom cli.parsers import args\n\nrun.main()\nargs.parse()\n"
_PLAIN_RUN = "def main():\n    return 0\n"
_IMPORTS_ARGS = "from cli.parsers import args\n\n\ndef main():\n    return args.parse()\n"
_IMPORTS_RUN = "from cli.commands import run\n\n\ndef parse():\n    return run.main()\n"
_versions = itertools.count(1)


def _edit(root: pathlib.Path, rel: str, text: str) -> None:
    """Write a production file whose (mtime_ns, size) differs from every earlier version."""
    path = write(root, rel, text)
    stamp = 1_800_000_000_000_000_000 + next(_versions) * 1_000_000
    os.utime(path, ns=(stamp, stamp))


def _place(root: pathlib.Path, text: str = TEST) -> Placement:
    """Where a test with this text belongs, judged against the checkout as it is now."""
    return placement.place("tests/test_x.py", ast.parse(text), placement.ModuleIndex(root))


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    locality.reset_caches()
    return make_repo(
        tmp_path,
        {
            "cli/parsers/__init__.py": "",
            ARGS: "def parse():\n    return None\n",
        },
    )


def test_without_a_dependency_the_home_is_the_common_ancestor(root: pathlib.Path) -> None:
    assert _place(root).home == "cli"


def test_a_package_that_directly_imports_the_other_module_is_the_home(
    root: pathlib.Path,
) -> None:
    _edit(root, RUN, _IMPORTS_ARGS)
    found = _place(root)
    assert (found.home, found.unit) == ("cli/commands", "cli")


def test_only_direct_imports_count_not_dependencies_of_dependencies(root: pathlib.Path) -> None:
    make_repo(
        root,
        {
            "cli/helpers/__init__.py": "",
            "cli/helpers/mid.py": "from cli.parsers import args\n",
            RUN: "from cli.helpers import mid\n\n\ndef main():\n    return mid\n",
        },
    )
    assert _place(root).home == "cli"


def test_a_function_level_import_counts(root: pathlib.Path) -> None:
    _edit(root, RUN, "def main():\n    from cli.parsers import args\n\n    return args.parse()\n")
    assert _place(root).home == "cli/commands"


def test_a_relative_import_counts(root: pathlib.Path) -> None:
    _edit(root, RUN, "from ..parsers import args\n\n\ndef main():\n    return args.parse()\n")
    assert _place(root).home == "cli/commands"


def test_importing_below_a_referenced_package_is_a_dependency_on_it(root: pathlib.Path) -> None:
    make_repo(
        root,
        {
            "cli/parsers/args/__init__.py": "",
            "cli/parsers/args/sub.py": "def parse():\n    return None\n",
            RUN: "from cli.parsers.args import sub\n\n\ndef main():\n    return sub\n",
        },
    )
    (root / ARGS).unlink()
    assert _place(root).home == "cli/commands"


def test_the_deepest_qualifying_package_is_the_home(root: pathlib.Path) -> None:
    make_repo(
        root,
        {
            "cli/commands/deep/__init__.py": "",
            "cli/commands/deep/leaf.py": "from cli.parsers import args\n",
            "cli/commands/other.py": "from cli.parsers import args\n",
        },
    )
    text = "from cli.commands.deep import leaf\nfrom cli.parsers import args\n"
    assert _place(root, text).home == "cli/commands/deep"


def test_a_dependency_cycle_keeps_the_home_at_the_common_ancestor(root: pathlib.Path) -> None:
    _edit(root, RUN, _IMPORTS_ARGS)
    _edit(root, ARGS, _IMPORTS_RUN)
    assert _place(root).home == "cli"


def test_a_package_that_holds_none_of_the_references_is_never_the_home(
    root: pathlib.Path,
) -> None:
    make_repo(
        root,
        {
            "cli/other/__init__.py": "",
            "cli/other/consumer.py": "from cli.commands import run\nfrom cli.parsers import args\n",
        },
    )
    assert _place(root).home == "cli"


def test_a_test_support_import_is_no_evidence_either_way(root: pathlib.Path) -> None:
    with_support = TEST + "from tests.support import fixtures\n\nfixtures.load()\n"
    assert _place(root, with_support).home == _place(root).home == "cli"
    _edit(root, RUN, _IMPORTS_ARGS)
    assert _place(root, with_support).home == _place(root).home == "cli/commands"


def test_a_lower_unit_that_imports_upward_never_becomes_the_home(root: pathlib.Path) -> None:
    """`base/net` importing both cli modules runs against the layers: only the home unit's own
    packages are candidates, so the edge cannot move the home out of `cli` or below its bound."""
    _edit(root, "base/net/retry.py", "from cli.commands import run\nfrom cli.parsers import args\n")
    text = TEST + "from base.net import retry\n\nretry.backoff()\n"
    found = _place(root, text)
    assert (found.home, found.unit) == ("cli", "cli")


def test_a_loose_service_module_keeps_the_services_directory_as_home(root: pathlib.Path) -> None:
    make_repo(root, {"services/__init__.py": "", "services/pidfile.py": "def read():\n    pass\n"})
    found = _place(root, "from services import pidfile\n\npidfile.read()\n")
    assert (found.home, found.unit) == ("services", "services.pidfile")


def test_the_home_follows_production_imports_added_and_removed(root: pathlib.Path) -> None:
    """Stability: adding an import lowers a home, deleting it raises it back, and completing a
    cycle raises it again; nothing else about the test changes."""
    assert _place(root).home == "cli"
    _edit(root, RUN, _IMPORTS_ARGS)
    assert _place(root).home == "cli/commands"
    _edit(root, RUN, _PLAIN_RUN)
    assert _place(root).home == "cli"
    _edit(root, RUN, _IMPORTS_ARGS)
    _edit(root, ARGS, _IMPORTS_RUN)
    assert _place(root).home == "cli"
    _edit(root, ARGS, "def parse():\n    return None\n")
    assert _place(root).home == "cli/commands"


# ------------------------------------------------------------------ the import cache

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOPS = placement.CODE_TOPS


def _edges(cache_root: pathlib.Path) -> dict[str, set[str]]:
    """Per production file, the modules its resolved imports name (through the shared cache)."""
    index = placement.ModuleIndex(cache_root)
    return {
        rel: {
            ref.module
            for ref in placement.collect_references(ast.parse(statements), index)
            if ref.kind == "import"
        }
        for rel, statements in cache.production_imports(cache_root, TOPS).items()
    }


@pytest.fixture
def cache_root(tmp_path: pathlib.Path) -> pathlib.Path:
    locality.reset_caches()
    return make_repo(
        tmp_path,
        {
            "cli/commands/run.py": (
                "import os\nfrom cli.commands import _util\n\n\n"
                "def main():\n    from base.net import retry\n\n    return retry\n"
            ),
            "cli/commands/tests/test_run.py": "from cli.commands import run\n",
            "cli/commands/docs/example.py": "from cli.commands import run\n",
            "cli/commands/sub/__init__.py": "from . import worker\nfrom .. import run\n",
            "cli/commands/sub/worker.py": "",
        },
    )


def _statements(cache_root: pathlib.Path) -> dict[str, str]:
    return cache.production_imports(cache_root, TOPS)


def test_it_keeps_the_absolute_first_party_import_statements(cache_root: pathlib.Path) -> None:
    found = _statements(cache_root)
    assert found["cli/commands/run.py"].splitlines() == [
        "from cli.commands import _util",
        "from base.net import retry",
    ]
    # relative imports are made absolute; `__init__.py` is its own package
    assert set(found["cli/commands/sub/__init__.py"].splitlines()) == {
        "from cli.commands.sub import worker",
        "from cli.commands import run",
    }
    assert found["cli/commands/sub/worker.py"] == ""


def test_test_and_docs_directories_are_not_production_source(cache_root: pathlib.Path) -> None:
    files = set(_statements(cache_root))
    assert "cli/commands/run.py" in files
    assert not {rel for rel in files if "/tests/" in rel or "/docs/" in rel}


@pytest.mark.parametrize("damage", ["not json", '{"version": 999, "files": {}}', '{"files": []}'])
def test_a_damaged_or_foreign_cache_is_rebuilt(cache_root: pathlib.Path, damage: str) -> None:
    fresh = _statements(cache_root)
    (cache_root / cache.CACHE_PATH).write_text(damage, encoding="utf-8")
    assert _statements(cache_root) == fresh
    assert json.loads((cache_root / cache.CACHE_PATH).read_text(encoding="utf-8"))["files"]


def test_previous_relative_import_semantics_cache_is_rebuilt(cache_root: pathlib.Path) -> None:
    """Version 2 predates fail-fast normalization of relative imports."""
    rel = "cli/commands/run.py"
    _edit(cache_root, rel, "from ..parsers import args\n")
    _statements(cache_root)
    cache_file = cache_root / cache.CACHE_PATH
    payload = json.loads(cache_file.read_text())
    payload["version"] = 2
    payload["files"][rel][2] = "from base.net import retry"
    cache_file.write_text(json.dumps(payload))
    assert _statements(cache_root)[rel] == "from cli.parsers import args"
    assert json.loads(cache_file.read_text())["version"] == 3


def test_a_cache_hit_still_resolves_against_the_current_tree(cache_root: pathlib.Path) -> None:
    """`from cli.commands import helper` names the attribute `helper` of `cli.commands` until a
    module `helper.py` appears next to it. The importing file did not change, so its cached
    statement is the same; the edge must still follow the tree."""
    _edit(cache_root, "cli/commands/run.py", "from cli.commands import helper\n")
    before = _edges(cache_root)["cli/commands/run.py"]
    assert before == {"cli.commands"}

    _edit(cache_root, "cli/commands/helper.py", "VALUE = 1\n")
    warm = _edges(cache_root)
    (cache_root / cache.CACHE_PATH).unlink()
    assert warm == _edges(cache_root)
    assert warm["cli/commands/run.py"] == {"cli.commands.helper"}


def test_a_cached_graph_is_the_graph_of_a_fresh_read_file_by_file(
    cache_root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real checkout: every file's cached statements equal a fresh parse of the file."""
    monkeypatch.setattr(cache, "CACHE_PATH", str(cache_root / "real-tree-cache.json"))
    cold = cache.production_imports(REPO_ROOT, TOPS)
    warm = cache.production_imports(REPO_ROOT, TOPS)
    assert cold == warm
    monkeypatch.setattr(cache, "CACHE_PATH", str(cache_root / "fresh-tree-cache.json"))
    assert warm == cache.production_imports(REPO_ROOT, TOPS)


def test_old_cache_cannot_hide_an_invalid_relative_import(cache_root: pathlib.Path) -> None:
    rel = "cli/commands/run.py"
    _edit(cache_root, rel, "from ...base.net import retry\n")
    path = cache_root / rel
    cache_file = cache_root / cache.CACHE_PATH
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {"version": 2, "files": {rel: [path.stat().st_mtime_ns, path.stat().st_size, ""]}}
        )
    )
    with pytest.raises(imports.InvalidRelativeImportError, match=f"{rel}:1"):
        _statements(cache_root)
