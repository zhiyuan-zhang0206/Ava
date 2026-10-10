"""File-loader evidence requires a real local execution chain."""

import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.imports import facts
from scripts.structure.tests.patch_repo import make_repo


@pytest.mark.parametrize("operation", ["", "mutate(spec)\n", "spec.loader = replacement\n"])
def test_unexecuted_or_changed_file_loader_remains_unknown(tmp_path: Path, operation: str) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": "import base.db.pool\n"})
    text = (
        "from pathlib import Path\n"
        "from importlib.util import spec_from_file_location, module_from_spec\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "spec = spec_from_file_location('probe', ROOT / 'base/net/probe.py')\n"
        "module = module_from_spec(spec)\n" + operation
    )
    if operation:
        text += "spec.loader.exec_module(module)\n"
    found = facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base", "ava", "cli"),
    )
    assert found.unknown
    assert found.file_executions == ()


def test_loaded_source_unknown_is_not_masked_by_known_execution(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": "helper().read_text()\n"})
    text = (
        "from pathlib import Path\n"
        "from importlib.util import spec_from_file_location, module_from_spec\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "spec = spec_from_file_location('probe', ROOT / 'base/net/probe.py')\n"
        "module = module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
    )
    found = facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base", "ava", "cli"),
    )
    assert len(found.file_executions) == 1
    assert any("base/net/probe.py:1" in gap.reason for gap in found.unknown)


_PREFIX = (
    "from pathlib import Path\n"
    "from importlib.util import spec_from_file_location, module_from_spec\n"
    "ROOT = Path(__file__).resolve().parents[2]\n"
    "spec = spec_from_file_location('probe', ROOT / 'base/net/probe.py')\n"
    "module = module_from_spec(spec)\n"
)


def test_parameter_loader_execution_retains_unknown(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    found = facts.collect(
        ast.parse("def execute(spec, module):\n spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


@pytest.mark.parametrize(
    "operation",
    [
        "alias = spec\nsink(alias)\nspec.loader.exec_module(module)\n",
        "container = [spec]\nsink(container)\nspec.loader.exec_module(module)\n",
        "sink(module)\nspec.loader.exec_module(module)\n",
        "spec = helper()\nspec.loader.exec_module(module)\n",
        "module = helper()\nspec.loader.exec_module(module)\n",
        "module.loader = replacement\nspec.loader.exec_module(module)\n",
        "if enabled:\n spec.loader.exec_module(module)\n",
        "for item in values:\n spec.loader.exec_module(module)\n",
    ],
)
def test_changed_escaped_or_conditional_chains_remain_unknown(
    tmp_path: Path, operation: str
) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": "import base.db.pool\n"})
    found = facts.collect(
        ast.parse(_PREFIX + operation),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


def test_file_source_uses_the_proven_runtime_namespace(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        {
            "base/net/probe.py": "import importlib\nimportlib.import_module(f'{__name__}.leaf')\n",
            "base/runtime/__init__.py": "",
            "base/runtime/leaf.py": "",
        },
    )
    text = _PREFIX.replace("'probe'", "'base.runtime'") + "spec.loader.exec_module(module)\n"
    found = facts.collect(
        ast.parse(text), "cli/tests/test_probe.py", placement.ModuleIndex(root), tops=("base",)
    )
    assert found.unknown == ()
    assert [(item.target, item.name) for item in found.file_executions] == [
        ("base/net/probe.py", "base.runtime")
    ]
    loaded = [fact for fact in found.records if fact.via == "file-loader"]
    assert all(fact.kind is facts.FactKind.RESOURCE for fact in loaded)
    assert "base/runtime/leaf.py" in {fact.target for fact in loaded}
    assert "base/runtime/__init__.py" in {fact.target for fact in loaded}


@pytest.mark.parametrize(
    "body",
    [
        "from . import retry\n",
        _PREFIX + "spec.loader.exec_module(module)\n",
    ],
)
def test_relative_or_nested_loaded_source_stays_unknown(tmp_path: Path, body: str) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": body})
    found = facts.collect(
        ast.parse(_PREFIX + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.file_executions
    assert found.unknown


def test_changed_path_builder_cannot_prove_the_executed_source(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": "import base.db.pool\n"})
    text = _PREFIX.replace("ROOT =", "Path.resolve = replacement\nROOT =")
    found = facts.collect(
        ast.parse(text + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


@pytest.mark.parametrize(
    "operation",
    [
        "holder.spec = spec\nspec.loader.exec_module(module)\n",
        "holder[0] = module\nspec.loader.exec_module(module)\n",
        "holder += [spec]\nspec.loader.exec_module(module)\n",
        "def capture():\n return spec\nsink(capture)\nspec.loader.exec_module(module)\n",
        "alias, = [spec]\nsink(alias)\nspec.loader.exec_module(module)\n",
        "(alias := spec)\nsink(alias)\nspec.loader.exec_module(module)\n",
    ],
)
def test_exported_or_unsupported_object_bindings_stay_unknown(
    tmp_path: Path, operation: str
) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": ""})
    found = facts.collect(
        ast.parse(_PREFIX + operation),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


def test_global_object_export_stays_unknown(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": ""})
    body = _PREFIX + "exported = spec\nspec.loader.exec_module(module)\n"
    text = "def execute():\n global exported\n" + "".join(
        " " + line + "\n" for line in body.splitlines()
    )
    found = facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


@pytest.mark.parametrize("body", [b"def broken(:", b"\xff", None])
def test_missing_or_invalid_loaded_source_stays_unknown(tmp_path: Path, body: bytes | None) -> None:
    root = make_repo(tmp_path)
    if body is not None:
        (root / "base/net/probe.py").write_bytes(body)
    found = facts.collect(
        ast.parse(_PREFIX + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


@pytest.mark.parametrize("linked", [False, True])
def test_non_python_or_symlink_source_stays_unknown(tmp_path: Path, linked: bool) -> None:
    root = make_repo(tmp_path, {"base/net/data.txt": "import base.db.pool\n"})
    text = _PREFIX
    if linked:
        (root / "base/net/probe.py").symlink_to(root / "base/net/retry.py")
    else:
        text = text.replace("probe.py", "data.txt")
    found = facts.collect(
        ast.parse(text + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


@pytest.mark.parametrize(
    "source",
    [
        _PREFIX.replace("ROOT / 'base/net/probe.py'", "helper()"),
        _PREFIX.replace("ROOT / 'base/net/probe.py'", "ROOT / target"),
        _PREFIX.replace("'probe', ROOT", "name='probe', location=ROOT"),
        _PREFIX.replace("module_from_spec(spec)", "module_from_spec(other)"),
        _PREFIX + "spec = helper()\n",
    ],
)
def test_opaque_or_mismatched_chain_inputs_stay_unknown(tmp_path: Path, source: str) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": ""})
    found = facts.collect(
        ast.parse(source + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown
    assert found.file_executions == ()


def test_plain_aliases_prove_execution_before_a_later_escape(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/net/probe.py": "import base.db.pool\n"})
    text = (
        _PREFIX + "alias = spec\nloaded = module\nalias.loader.exec_module(loaded)\nsink(alias)\n"
    )
    found = facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert found.unknown == ()
    assert len(found.file_executions) == 1
    assert "base/db/pool.py" in {fact.target for fact in found.records}


def test_finite_names_and_paths_keep_conservative_pairs_and_unrelated_gaps(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"base/net/one.py": "", "base/net/two.py": ""})
    body = (
        _PREFIX.replace("'probe'", "f'probe_{target}'").replace(
            "'base/net/probe.py'", "f'base/net/{target}.py'"
        )
        + "spec.loader.exec_module(module)\n"
    )
    text = (
        "import pytest\nfrom importlib.util import spec_from_file_location\n"
        "spec_from_file_location('other', helper())\n"
        "@pytest.mark.parametrize('target', ['one', 'two'])\n"
        "def execute(target):\n" + "".join(" " + line + "\n" for line in body.splitlines())
    )
    found = facts.collect(
        ast.parse(text),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base",),
    )
    assert {(item.name, item.target) for item in found.file_executions} == {
        (f"probe_{name}", f"base/net/{path}.py")
        for name in ("one", "two")
        for path in ("one", "two")
    }
    assert len(found.unknown) == 1
    assert "no proven execution chain" in found.unknown[0].reason


def test_loaded_resources_do_not_grant_python_subject_or_root_ownership(tmp_path: Path) -> None:
    from scripts.structure import placement_evidence

    root = make_repo(tmp_path, {"base/net/probe.py": "import base.db.pool\n"})
    proof = placement_evidence.subject_lca(
        ast.parse(_PREFIX + "spec.loader.exec_module(module)\n"),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
    )
    assert proof.unknown == ()
    assert proof.modules == ()
    assert proof.directory is None
