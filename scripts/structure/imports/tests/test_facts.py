"""Unpruned dependency facts retain bounded inputs and precise lexical origins."""

import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.imports import facts
from scripts.structure.tests.patch_repo import make_repo


def evidence(root: Path, text: str, path: str = "cli/tests/test_probe.py") -> facts.Evidence:
    return facts.collect(
        ast.parse(text), path, placement.ModuleIndex(root), tops=("base", "ava", "cli")
    )


def test_relative_symbol_and_submodule_imports_have_exact_doors(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    found = evidence(
        root, "from .. import retry\nfrom ..retry import backoff\n", "base/net/tests/test_retry.py"
    )
    assert [fact.target for fact in found.records] == ["base.net.retry", "base.net.retry"]
    assert found.unknown == ()
    index = placement.ModuleIndex(root)
    assert index.file("base.net.retry") == "base/net/retry.py"
    assert index.file("base") == "base/__init__.py"
    assert index.file("base.missing") is None


def test_finite_pytest_domain_resolves_fstring_import(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    found = evidence(
        root,
        'import pytest, importlib\n@pytest.mark.parametrize("part", ["retry"])\ndef test_load(part):\n importlib.import_module(f"base.net.{part}")\n',
    )
    assert [(fact.kind, fact.target) for fact in found.records] == [
        (facts.FactKind.DYNAMIC_IMPORT, "base.net.retry")
    ]
    assert found.unknown == ()


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.mark.parametrize('part', values)",
        "@pytest.mark.parametrize('part', ['retry'], indirect=True)",
    ],
)
def test_unproven_parameter_domains_remain_unknown(tmp_path: Path, decorator: str) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import pytest, importlib\n"
        + decorator
        + "\ndef test_load(part):\n importlib.import_module(f'base.net.{part}')\n",
    )
    assert found.records == ()
    assert len(found.unknown) == 1


def test_relative_literal_dynamic_import_and_opaque_input(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from importlib import import_module as load\nload('.retry', 'base.net')\nload('requests')\ndef opaque(name):\n load(name)\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert len(found.unknown) == 1
    assert found.unknown[0].line == 5


def test_local_shadow_does_not_pollute_global_dynamic_alias(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import importlib as loader\ndef unrelated(loader):\n loader.import_module('ava.missing')\nloader.import_module('base.net.retry')\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_literal_python_module_and_embedded_imports_share_resolution(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import sys, subprocess\nsubprocess.run([sys.executable, '-m', 'base.net.retry'])\nsubprocess.run([sys.executable, '-c', 'from base.db import pool'])\n",
    )
    assert {(fact.kind, fact.target) for fact in found.records} == {
        (facts.FactKind.PYTHON_MODULE, "base.net.retry"),
        (facts.FactKind.EMBEDDED_IMPORT, "base.db.pool"),
    }
    assert found.unknown == ()


def test_resource_chain_keeps_only_final_path_and_known_missing_target(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n(ROOT / 'base' / 'future.txt').read_text()\n",
    )
    assert [(fact.kind, fact.target) for fact in found.records] == [
        (facts.FactKind.RESOURCE, "base/future.txt")
    ]
    assert found.unknown == ()


def test_resource_root_shadow_is_lexical(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\ndef unrelated(ROOT):\n return ROOT / 'opaque.txt'\n(ROOT / 'base/net/retry.py').read_text()\n",
    )
    assert [fact.target for fact in found.records] == ["base/net/retry.py"]


def test_unknown_import_prefix_is_not_a_resolved_parent(tmp_path: Path) -> None:
    found = evidence(make_repo(tmp_path), "import base.net.missing\n")
    assert found.records == ()
    assert found.unknown[0].kind == facts.FactKind.IMPORT
