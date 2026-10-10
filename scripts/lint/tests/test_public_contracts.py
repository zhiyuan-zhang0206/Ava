"""Public component contracts enforce source ownership rather than spelling alone."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from scripts.lint.public_contracts import (
    Component,
    Contracts,
    audit_module,
    entry_members,
    read_components,
)
from scripts.structure.placement import CODE_TOPS, ModuleIndex


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


@pytest.fixture
def contracts(tmp_path: Path) -> Contracts:
    _write(
        tmp_path,
        "base/store/api.py",
        "__all__ = ['Store', 'read']\nclass Store: pass\ndef read(): pass\ndef _helper(): pass\n",
    )
    _write(tmp_path, "base/store/impl.py", "def hidden(): pass\n")
    _write(tmp_path, "base/store/_state.py", "value = 1\n")
    _write(tmp_path, "base/store/child/api.py", "__all__ = ['read']\ndef read(): pass\n")
    _write(tmp_path, "base/client/use.py", "")
    _write(tmp_path, "tests/client/test_use.py", "")
    return Contracts(
        (
            Component("base.store", ("base.store.api",)),
            Component("base.store.child", ("base.store.child.api",)),
            Component("base.client", ()),
            Component("tests.client", ()),
        ),
        ModuleIndex(tmp_path),
        ("base", "tests"),
    )


def _reasons(contracts: Contracts, source: str, path: str = "base/client/use.py") -> list[str]:
    return [violation.reason for violation in audit_module(ast.parse(source), path, contracts)]


@pytest.mark.parametrize(
    "source",
    [
        "from base.store.api import _helper",
        "from base.store.api import _helper as public",
        "import base.store._state as public",
        "import base.store.api as door\nalias = door\nalias._helper()",
        "from base.store.api import Store\nStore._internal()",
        "from base.store.api import Store\ninstance = Store()\ninstance._internal()",
        "import base.store.api as door\ngetattr(door, '_helper')()",
        "from importlib import import_module\nimport_module('base.store._state')",
        "from importlib import import_module\nimport_module('base.store.api')._helper()",
        "from importlib import import_module\nm = import_module('base.store.api')\nm._helper()",
        "from unittest.mock import patch\npatch('base.store.api._helper')",
    ],
)
def test_private_reach_is_forbidden_across_files(contracts: Contracts, source: str) -> None:
    assert "Private names and modules are file-local" in _reasons(contracts, source)


@pytest.mark.parametrize(
    "patch",
    [
        "patch.object(door, '_helper')",
        "patch.multiple(door, _helper=None)",
        "alias = patch.object\nalias(door, '_helper')",
        "monkeypatch.setattr(door, '_helper', None)",
        "monkeypatch.setattr('base.store.api._helper', None)",
    ],
)
def test_patch_grammar_cannot_bypass_file_privacy(contracts: Contracts, patch: str) -> None:
    source = "from unittest.mock import patch\nimport base.store.api as door\n" + patch
    assert "Private names and modules are file-local" in _reasons(contracts, source)


def test_patch_object_respects_lexical_parameter_shadow(contracts: Contracts) -> None:
    source = "from unittest.mock import patch\nimport base.store.api as door\n"
    source += "def local(door):\n    patch.object(door, 'public')"
    assert _reasons(contracts, source) == []


@pytest.mark.parametrize("path", ["base/store/impl.py", "tests/client/test_use.py"])
def test_same_component_and_tests_do_not_inherit_private_access(
    contracts: Contracts,
    path: str,
) -> None:
    assert _reasons(contracts, "from base.store.api import _helper", path) == [
        "Private names and modules are file-local"
    ]


def test_actual_defining_file_retains_its_own_private_access(contracts: Contracts) -> None:
    assert (
        _reasons(
            contracts,
            "import base.store.api as self_module\nself_module._helper()",
            "base/store/api.py",
        )
        == []
    )


def test_non_private_spelling_is_not_a_public_contract(contracts: Contracts) -> None:
    assert _reasons(contracts, "from base.store.impl import hidden") == [
        "Cross-component access requires an entry module"
    ]


def test_public_entry_and_same_component_implementation_access(contracts: Contracts) -> None:
    assert _reasons(contracts, "from base.store.api import Store, read") == []
    assert _reasons(contracts, "from base.store.impl import hidden", "base/store/api.py") == []


def test_non_exported_member_of_entry_owner_fails(contracts: Contracts) -> None:
    _write(
        contracts.index.repo_root,
        "base/store/api.py",
        "__all__ = ['read']\ndef read(): pass\ndef extra(): pass\n",
    )
    assert _reasons(contracts, "from base.store.api import extra") == [
        "Member is not in the entry owner's __all__"
    ]


def test_child_component_does_not_inherit_parent_internals(contracts: Contracts) -> None:
    assert _reasons(contracts, "from base.store.impl import hidden", "base/store/child/api.py") == [
        "Cross-component access requires an entry module"
    ]


def test_unclassified_component_is_not_default_public(contracts: Contracts) -> None:
    assert _reasons(contracts, "from base.store.api import read", "base/unknown/use.py") == [
        "Unclassified component boundary"
    ]


def test_relative_imports_use_the_actual_definition_owner(contracts: Contracts) -> None:
    assert _reasons(contracts, "from .api import _helper", "base/store/impl.py") == [
        "Private names and modules are file-local"
    ]


def test_other_lexical_scope_does_not_erase_module_provenance(contracts: Contracts) -> None:
    source = "import base.store.api as door\ndef unrelated(door): pass\ndoor._helper()"
    assert _reasons(contracts, source) == ["Private names and modules are file-local"]


def test_parameter_shadow_is_not_assumed_to_be_the_imported_module(contracts: Contracts) -> None:
    source = "import base.store.api as door\ndef unrelated(door):\n    return door.value"
    assert _reasons(contracts, source) == []


@pytest.mark.parametrize("name", ["__dataclass_fields__", "__custom__", "__all_for_ava__"])
def test_custom_dunder_is_not_a_language_protocol(contracts: Contracts, name: str) -> None:
    assert _reasons(contracts, f"from base.store.api import Store\nStore.{name}") == [
        "Private names and modules are file-local"
    ]


def test_language_protocol_and_module_metadata_have_exact_semantics(contracts: Contracts) -> None:
    assert _reasons(contracts, "from base.store.api import Store\nStore.__repr__") == []
    assert _reasons(contracts, "import base.store.api as door\ndoor.__name__") == []
    assert _reasons(contracts, "import base.store.api as door\ndoor.__repr__") == []
    assert _reasons(contracts, "import base.store.api as door\ndoor.__dict__") == []
    assert _reasons(contracts, "from base.store.api import Store\nStore.__match_args__") == []


@pytest.mark.parametrize(
    "expression",
    [
        "door.__dict__['_helper']",
        "vars(door)['_helper']",
        "door.__dict__.get('_helper')",
        "namespace = vars(door)\nnamespace['_helper']",
        "namespace = vars(door)\nnamespace.get('_helper')",
    ],
)
def test_language_namespace_metadata_does_not_launder_private_keys(
    contracts: Contracts,
    expression: str,
) -> None:
    assert _reasons(contracts, "import base.store.api as door\n" + expression) == [
        "Private names and modules are file-local"
    ]


@pytest.mark.parametrize("operation", ["setattr", "delattr", "hasattr"])
def test_builtin_reflection_cannot_bypass_member_privacy(
    contracts: Contracts, operation: str
) -> None:
    arguments = "door, '_helper', None" if operation == "setattr" else "door, '_helper'"
    source = f"import base.store.api as door\n{operation}({arguments})"
    assert _reasons(contracts, source) == ["Private names and modules are file-local"]


def test_shadowed_reflection_is_not_assumed_to_be_a_builtin(contracts: Contracts) -> None:
    source = "import base.store.api as door\ndef setattr(obj, attr, value): pass\n"
    assert _reasons(contracts, source + "setattr(door, '_helper', None)") == []


def test_literal_dynamic_execution_uses_shared_facts(contracts: Contracts) -> None:
    source = "import sys, subprocess\nsubprocess.run([sys.executable, '-m', 'base.store._state'])"
    assert "Private names and modules are file-local" in _reasons(contracts, source)


def test_unknown_dynamic_import_is_an_error(contracts: Contracts) -> None:
    source = "import importlib\ndef load(name):\n    return importlib.import_module(name)"
    assert any(reason.startswith("Unproved import:") for reason in _reasons(contracts, source))


@pytest.mark.parametrize(
    "code",
    [
        "from base.store.api import _helper as public",
        "import base.store.api as door; door._helper()",
        "from .api import _helper",
        "import importlib; importlib.import_module('base.store.api')._helper()",
    ],
)
def test_executed_python_members_do_not_launder_module_only_facts(
    contracts: Contracts,
    code: str,
) -> None:
    source = f"import sys, subprocess\nsubprocess.run([sys.executable, '-c', {code!r}])"
    assert any(reason.startswith("Python -c line") for reason in _reasons(contracts, source))


def test_executed_python_is_distinct_from_the_launcher_definition_file(
    contracts: Contracts,
) -> None:
    code = "from base.store.api import _helper"
    source = f"import sys, subprocess\nsubprocess.run([sys.executable, '-c', {code!r}])"
    assert any(
        reason.startswith("Python -c line")
        for reason in _reasons(contracts, source, "base/store/api.py")
    )


def test_invalid_executed_source_is_retained_as_a_diagnostic(contracts: Contracts) -> None:
    source = "import sys, subprocess\nsubprocess.run([sys.executable, '-c', 'broken ('])"
    assert any(reason.startswith("Unproved import:") for reason in _reasons(contracts, source))


@pytest.mark.parametrize(
    "code",
    [
        "from base.store.api import _helper",
        "import base.store.api as door; door._helper()",
        "import importlib; importlib.import_module(name)",
    ],
)
def test_nested_execution_does_not_hide_members_or_recognized_gaps(
    contracts: Contracts, code: str
) -> None:
    launcher = f"import sys, subprocess; subprocess.run([sys.executable, '-c', {code!r}])"
    source = f"import sys, subprocess; subprocess.run([sys.executable, '-c', {launcher!r}])"
    assert any(reason.startswith("Python -c line") for reason in _reasons(contracts, source))


@pytest.mark.parametrize(
    "source",
    [
        "from base.store.impl import hidden\n__all__ = ['hidden']",
        "from base.store.impl import hidden as read\n__all__ = ['read']",
        "from base.store.impl import hidden\nread = hidden\n__all__ = ['read']",
        "__all__ = list(['read'])\ndef read(): pass",
        "__all__ = ['_helper']\ndef _helper(): pass",
        "__all__ = ['read', 'read']\ndef read(): pass",
        "__all__ = ['read']\ndef read(): pass\n__all__ += ['extra']",
        "__all__ = ['read']\ndef read(): pass\n__all__.append('extra')",
        "def read(): pass\nfrom base.store.impl import hidden as read\n__all__ = ['read']",
        "__all__ = ['read']\ndef read(): pass\n__all__[0] = 'extra'",
        "__all__ = ['read']\ndef read(): pass\ndel __all__[:]",
        "__all__ = ['read']\ndef read(): pass\nalias = __all__\nalias.append('extra')",
        "__all__ = ['read']\ndef read(): pass\nif True: __all__ = ['extra']",
        "if False: __all__ = ['read']\ndef read(): pass",
        "__all__ = alias = ['read']\ndef read(): pass\nalias.append('extra')",
        "def read(): pass\nfrom base.store.impl import hidden\nread = hidden\n__all__=['read']",
        "read = 3\nfrom base.store import impl\nread = impl.hidden\n__all__=['read']",
        "read: int\n__all__=['read']",
    ],
)
def test_entry_owner_cannot_be_a_barrel_or_dynamic_export_list(source: str) -> None:
    with pytest.raises(ValueError):
        entry_members(ast.parse(source))


def test_entry_exports_are_direct_definitions() -> None:
    source = "__all__ = ('Store', 'read', 'LIMIT')\nclass Store: pass\n"
    source += "def read(): pass\nLIMIT = 3"
    assert entry_members(ast.parse(source)) == {"Store", "read", "LIMIT"}


def test_annotation_without_assignment_does_not_replace_an_existing_definition() -> None:
    source = "read = 3\nread: int\n__all__=['read']"
    assert entry_members(ast.parse(source)) == {"read"}


@pytest.mark.parametrize(
    "record",
    [
        {"module": "base.store", "entry_modules": ["base.store.**"]},
        {"module": "base.store", "entry_modules": ["base.other.api"]},
        {"module": "base.store", "entry_modules": ["base.store.api"], "allowed": ["tests"]},
    ],
)
def test_declarations_do_not_grant_wildcards_or_consumer_allowlists(
    record: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        read_components({"components": [record]})


def test_declaration_schema_has_one_component_owner() -> None:
    components = read_components(
        {"components": [{"module": "base.store", "entry_modules": ["base.store.api"]}]}
    )
    assert components == (Component("base.store", ("base.store.api",)),)


def test_unused_missing_entry_is_still_a_contract_error(contracts: Contracts) -> None:
    invalid = Contracts(
        (Component("base.store", ("base.store.missing",)),), contracts.index, contracts.tops
    )
    assert invalid.validate()[0].reason == "Entry owner has no source file: base.store.missing"


def test_parent_cannot_declare_a_child_component_entry(contracts: Contracts) -> None:
    invalid = Contracts(
        (
            Component("base.store", ("base.store.child.api",)),
            Component("base.store.child", ("base.store.child.api",)),
        ),
        contracts.index,
        contracts.tops,
    )
    assert invalid.validate()[0].reason == "Parent component cannot grant a child's entry"


def test_checkout_entries_have_definition_owners_and_checker_uses_public_contracts() -> None:
    root = Path(__file__).resolve().parents[3]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    components = read_components(config["tool"]["ava"]["public_contracts"])
    contracts = Contracts(
        components,
        ModuleIndex(root),
        tuple(dict.fromkeys((*CODE_TOPS, *(item.module.split(".")[0] for item in components)))),
    )
    assert contracts.validate() == ()
    for path in (
        "scripts/lint/public_contracts.py",
        "scripts/lint/tests/test_public_contracts.py",
    ):
        assert audit_module(ast.parse((root / path).read_text()), path, contracts) == ()


@pytest.mark.parametrize(
    "source",
    [
        "from scripts.lint.code_structure import main\n",
        "from base.host.proc import run_bounded\n",
        "from tests.e2e.fixture_environment import E2EEnv\n",
        "from tests.e2e.fakes.scripted_model import ScriptExhaustedError, ScriptedFakeChatModel\n",
        "from tests.e2e.fakes.scenario_recording import RecordingModel, model_inputs, reset_record\n",
        "from cli.main import main\n",
        "from cli.parsers import build_parser, command_options, parse_args\n",
    ],
)
def test_integrated_entry_consumers_resolve_the_actual_definition_owner(source: str) -> None:
    root = Path(__file__).resolve().parents[3]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    components = read_components(config["tool"]["ava"]["public_contracts"])
    contracts = Contracts(
        components,
        ModuleIndex(root),
        tuple(dict.fromkeys((*CODE_TOPS, *(item.module.split(".")[0] for item in components)))),
    )
    assert (
        audit_module(ast.parse(source), "scripts/lint/tests/test_public_contracts.py", contracts)
        == ()
    )
