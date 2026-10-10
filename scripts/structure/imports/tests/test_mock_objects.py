"""Mock object targets retain their import edges without inventing string imports."""

import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.imports import facts
from scripts.structure.tests.patch_repo import make_repo


def evidence(root: Path, text: str) -> facts.Evidence:
    return facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=placement.CODE_TOPS,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\npatch.dict(os.environ, {})",
        "import sys as system\npatch.dict(in_dict=system.modules, values={})",
        "from os import environ as environment\npatch.dict(environment, {})",
        "import os\nvalues = os.environ\npatch.dict(values, {})",
    ],
)
def test_stdlib_mapping_object_does_not_use_string_importer(tmp_path: Path, source: str) -> None:
    found = evidence(make_repo(tmp_path), "from unittest.mock import patch\n" + source)
    assert found.records == found.unknown == ()


@pytest.mark.parametrize(
    "source",
    [
        "import base.net.retry as target\npatch.multiple(target, backoff=None)",
        "import base.net.retry\npatch.multiple(base.net.retry, backoff=None)",
        "from base.net import retry as target\npatch.multiple(target, backoff=None)",
        "from base.net import retry\ntarget = retry\npatch.multiple(target, backoff=None)",
    ],
)
def test_module_object_keeps_static_import_without_dynamic_gap(tmp_path: Path, source: str) -> None:
    found = evidence(make_repo(tmp_path), "from unittest.mock import patch\n" + source)
    assert [(record.kind, record.target) for record in found.records] == [
        (facts.FactKind.IMPORT, "base.net.retry")
    ]
    assert found.unknown == ()


def test_namespace_submodule_object_has_no_exporting_package_door(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/namespace/worker.py": ""})
    found = evidence(
        root,
        "from unittest.mock import patch\nfrom base.namespace import worker\n"
        "patch.multiple(worker, run=None)\n",
    )
    assert [record.target for record in found.records] == ["base.namespace.worker"]
    assert found.unknown == ()


def test_external_module_object_does_not_need_a_repository_source(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path), "from unittest.mock import patch\nimport os\npatch.multiple(os)\n"
    )
    assert found.records == found.unknown == ()


def test_unloaded_module_attribute_is_not_proven_by_file_existence(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from unittest.mock import patch\nimport base\npatch.multiple(base.net.retry)\n",
    )
    assert len(found.unknown) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef run(os):\n return patch.dict(os.environ)",
        "import os\nos = source\npatch.dict(os.environ)",
        "import os\nvalues = os.environ\nvalues = source\npatch.dict(values)",
        "import os\nos.environ = source\npatch.dict(os.environ)",
        "import os\ndef run():\n os.environ = source\n return patch.dict(os.environ)",
        "import os as system\nfrom os import environ\nsystem.environ = source\npatch.dict(environ)",
        "import base.net.retry as target\ndef run(target):\n return patch.multiple(target)",
        "from base.net import retry\ndef run(target):\n return patch.multiple(target)",
    ],
)
def test_unproven_object_target_stays_unknown(tmp_path: Path, source: str) -> None:
    found = evidence(make_repo(tmp_path), "from unittest.mock import patch\n" + source)
    assert len(found.unknown) == 1
    assert "not bounded literal text" in found.unknown[0].reason


@pytest.mark.parametrize(
    "package",
    [
        "retry = 'base.net.retry.REGISTRY'\n",
        "from somewhere import retry\n",
        "def __getattr__(name):\n return 'base.net.retry.REGISTRY'\n",
    ],
)
def test_from_package_member_can_be_string_even_when_submodule_exists(
    tmp_path: Path, package: str
) -> None:
    root = make_repo(tmp_path, {"base/net/__init__.py": package})
    found = evidence(
        root,
        "from unittest.mock import patch\nfrom base.net import retry\npatch.multiple(retry)\n",
    )
    assert len(found.unknown) == 1


def test_imported_string_symbol_does_not_become_object_by_module_prefix(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from unittest.mock import patch\nfrom base.net.retry import TARGET\npatch.dict(TARGET)\n",
    )
    assert len(found.unknown) == 1


@pytest.mark.parametrize("callee", ["patch.dict", "patch.multiple"])
def test_string_forms_continue_to_import_target(tmp_path: Path, callee: str) -> None:
    found = evidence(
        make_repo(tmp_path),
        f"from unittest.mock import patch\n{callee}('base.net.retry.REGISTRY')\n",
    )
    assert [(record.kind, record.target) for record in found.records] == [
        (facts.FactKind.DYNAMIC_IMPORT, "base.net.retry")
    ]
    assert found.unknown == ()


def test_plain_patch_still_requires_a_string_import_target(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path), "from unittest.mock import patch\nimport os\npatch(os.environ)\n"
    )
    assert len(found.unknown) == 1


def test_unrelated_function_shadow_does_not_hide_mapping_object(tmp_path: Path) -> None:
    found = evidence(
        make_repo(tmp_path),
        "from unittest.mock import patch\nimport os\ndef unrelated(os):\n return os\n"
        "patch.dict(os.environ)\n",
    )
    assert found.records == found.unknown == ()
