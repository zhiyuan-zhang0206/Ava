from __future__ import annotations

import ast
import itertools
import json
import os
import pathlib

import pytest

from scripts.structure import imports, placement
from scripts.structure.imports import cache
from scripts.structure.tests.patch_repo import make_repo, write

RUN = "cli/commands/run.py"
ARGS = "cli/parsers/args.py"
TEST = "from cli.commands import run\nfrom cli.parsers import args\n\nrun.main()\nargs.parse()\n"
_PLAIN_RUN = "def main():\n    return 0\n"
_IMPORTS_ARGS = "from cli.parsers import args\n\n\ndef main():\n    return args.parse()\n"
_IMPORTS_RUN = "from cli.commands import run\n\n\ndef parse():\n    return run.main()\n"
_versions = itertools.count(1)


def _edit(root: pathlib.Path, rel: str, text: str) -> None:
    path = write(root, rel, text)
    stamp = 1_800_000_000_000_000_000 + next(_versions) * 1_000_000
    os.utime(path, ns=(stamp, stamp))


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_repo(
        tmp_path,
        {
            "cli/parsers/__init__.py": "",
            ARGS: "def parse():\n    return None\n",
        },
    )


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOPS = placement.CODE_TOPS


def _edges(cache_root: pathlib.Path) -> dict[str, set[str]]:
    index = placement.ModuleIndex(cache_root)
    return {
        rel: {
            ref.module
            for ref in placement.collect_references(ast.parse(statements), index)
            if ref.kind == "import"
        }
        for rel, statements in cache.production_imports(
            cache_root, TOPS, read_source=lambda path: path.read_text(encoding="utf-8")
        ).items()
    }


@pytest.fixture
def cache_root(tmp_path: pathlib.Path) -> pathlib.Path:
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
    return cache.production_imports(
        cache_root, TOPS, read_source=lambda path: path.read_text(encoding="utf-8")
    )


def test_it_keeps_the_absolute_first_party_import_statements(cache_root: pathlib.Path) -> None:
    found = _statements(cache_root)
    assert found["cli/commands/run.py"].splitlines() == [
        "from cli.commands import _util",
        "from base.net import retry",
    ]
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
    monkeypatch.setattr(cache, "CACHE_PATH", str(cache_root / "real-tree-cache.json"))
    cold = cache.production_imports(
        REPO_ROOT, TOPS, read_source=lambda path: path.read_text(encoding="utf-8")
    )
    warm = cache.production_imports(
        REPO_ROOT, TOPS, read_source=lambda path: path.read_text(encoding="utf-8")
    )
    assert cold == warm
    monkeypatch.setattr(cache, "CACHE_PATH", str(cache_root / "fresh-tree-cache.json"))
    assert warm == cache.production_imports(
        REPO_ROOT, TOPS, read_source=lambda path: path.read_text(encoding="utf-8")
    )


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
