"""Unpruned dependency facts retain bounded inputs and precise lexical origins."""

import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.imports import executed, facts
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


@pytest.mark.parametrize("shadow", ["Path", "__file__"])
def test_resource_root_requires_real_path_constructor_and_file_anchor(
    tmp_path: Path, shadow: str
) -> None:
    found = evidence(
        make_repo(tmp_path),
        f"from pathlib import Path\ndef unrelated({shadow}):\n return Path(__file__).resolve().parents[2] / 'base/data.txt'\n",
    )
    assert found.records == ()


def test_resource_constructor_alias_is_supported(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path as FilePath\n(FilePath(__file__).resolve().parents[2] / 'base/data.txt').read_text()\n",
    )
    assert [fact.target for fact in found.records] == ["base/data.txt"]


def test_execution_unknowns_retain_their_kinds(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import sys, subprocess\ndef launch(target):\n subprocess.run([sys.executable, '-m', target])\n subprocess.run([sys.executable, '-c', target])\n",
    )
    assert {item.kind for item in found.unknown} == {
        facts.FactKind.PYTHON_MODULE,
        facts.FactKind.EMBEDDED_IMPORT,
    }


def test_assignment_alias_retains_import_origin(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import importlib\nload = importlib.import_module\nagain = load\nagain('base.net.retry')\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_embedded_dynamic_import_uses_the_same_binding_resolver(tmp_path: Path) -> None:
    code = "import importlib\nname = 'base.net.retry'\nload = importlib.import_module\nload(name)\n"
    found = evidence(
        make_repo(tmp_path),
        "import sys, subprocess\nsubprocess.run([sys.executable, '-c', " + repr(code) + "])\n",
    )
    assert [(fact.line, fact.kind, fact.target) for fact in found.records] == [
        (2, facts.FactKind.EMBEDDED_IMPORT, "base.net.retry")
    ]
    assert found.unknown == ()


@pytest.mark.parametrize(
    "read",
    [
        "Path('base/data.txt').read_text()",
        "open('base/data.txt')",
        "Path(name).read_bytes()",
        "(ROOT / name).read_text()",
    ],
)
def test_recognized_resource_read_without_anchor_is_unknown(tmp_path: Path, read: str) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n" + read,
    )
    assert found.records == ()
    assert len(found.unknown) == 1
    assert found.unknown[0].kind == facts.FactKind.RESOURCE


def test_known_external_absolute_resource_does_not_depend_on_checkout(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path), "from pathlib import Path\nPath('/etc/hosts').read_text()\n"
    )
    assert found.records == found.unknown == ()


def test_repository_root_traversal_records_the_directory(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\nROOT.rglob('*.py')\n",
    )
    assert [fact.target for fact in found.records] == ["."]


def test_function_default_uses_parent_scope_before_parameter_binding(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import importlib\ndef load(importlib=importlib.import_module('base.net.retry')):\n return importlib\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_class_base_uses_parent_scope_before_class_local_import(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import importlib\nclass Example(importlib.import_module('base.net.retry')):\n import third_party as importlib\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]


def test_file_relative_resource_has_a_proven_checkout_anchor(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\n(Path(__file__).parent / 'data.txt').read_text()\n",
    )
    assert [fact.target for fact in found.records] == ["cli/tests/data.txt"]
    assert found.unknown == ()


@pytest.mark.parametrize(
    "expression",
    [
        "[importlib for importlib in []]",
        "{importlib for importlib in []}",
        "{importlib: None for importlib in []}",
        "(importlib for importlib in [])",
    ],
)
def test_comprehension_binding_does_not_hide_enclosing_import(
    tmp_path: Path, expression: str
) -> None:
    found = evidence(
        make_repo(tmp_path),
        f"import importlib\n{expression}\nimportlib.import_module('base.net.retry')\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_comprehension_first_iterator_uses_parent_but_body_uses_local_scope(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import importlib\n[importlib.import_module('ava') "
        "for importlib in importlib.import_module('base.net.retry')]\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_comprehension_does_not_hide_following_python_launcher(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "import sys, subprocess\n[subprocess for subprocess in []]\n"
        "subprocess.run([sys.executable, '-c', 'import base.net.retry'])\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


@pytest.mark.parametrize("callee", ["replace", "replace.multiple", "replace.dict"])
def test_literal_mock_patch_target_is_a_runtime_import(tmp_path: Path, callee: str) -> None:
    found = evidence(
        make_repo(tmp_path),
        f"from unittest.mock import patch as replace\n{callee}('base.net.retry._sleep')\n",
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


def test_opaque_mock_patch_target_retains_its_runtime_gap(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from unittest.mock import patch\ndef load(target):\n return patch(target)\n",
    )
    assert found.records == ()
    assert len(found.unknown) == 1
    assert found.unknown[0].line == 3


def test_local_function_named_patch_is_not_a_dynamic_import(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path), "def patch(target):\n return target\npatch('base.net.retry._sleep')\n"
    )
    assert found.records == found.unknown == ()


def test_mock_dict_object_target_does_not_invoke_a_string_import(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path), "from unittest.mock import patch\nvalues = {}\npatch.dict(values)\n"
    )
    assert found.records == found.unknown == ()


def test_mock_patch_runtime_edge_keeps_ownership_policy_separate(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    tree = ast.parse(
        "import ava\nfrom unittest.mock import patch as replace\nreplace('base.net.retry._sleep')\n"
    )
    refs, fallback = placement.placement_references(tree, placement.ModuleIndex(root))
    assert [ref.module for ref in refs if ref.kind in placement.STRONG_KINDS] == ["ava"]
    assert not fallback


def test_unsupported_path_operation_read_is_explicitly_unknown(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from pathlib import Path\nPath(__file__).with_name('data.txt').read_text()\n",
    )
    assert len(found.unknown) == 1
    assert found.unknown[0].kind == facts.FactKind.RESOURCE


def test_no_launcher_avoids_repeating_execution_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_execution_analysis(*_args: object) -> executed.Inputs:
        pytest.fail("A source without a launcher needs no execution analysis")

    monkeypatch.setattr(executed, "inputs", refuse_execution_analysis)
    found = evidence(
        make_repo(tmp_path), "import importlib\nimportlib.import_module('base.net.retry')\n"
    )
    assert [fact.target for fact in found.records] == ["base.net.retry"]
    assert found.unknown == ()


@pytest.mark.parametrize(
    "source, unresolved",
    [
        ("subprocess.run([sys.executable, '-c', 'import base.net.retry'])", False),
        ("def run():\n subprocess.run([sys.executable, '-c', code])", True),
        ("[subprocess.run([sys.executable, '-c', 'import base.net.retry']) for _ in []]", False),
    ],
)
def test_launchers_retain_execution_analysis_in_every_lexical_scope(
    tmp_path: Path, source: str, unresolved: bool
) -> None:
    found = evidence(make_repo(tmp_path), "import sys, subprocess\n" + source)
    assert bool(found.unknown) is unresolved
    assert [fact.target for fact in found.records] == ([] if unresolved else ["base.net.retry"])
