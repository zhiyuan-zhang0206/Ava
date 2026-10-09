"""The placement rule: a test's home comes from its own first-party references."""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import imports, locality, patch_points, patch_targets, placement
from scripts.structure.tests.patch_repo import make_repo, write


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    locality.reset_caches()
    return make_repo(tmp_path)


def _place(root: pathlib.Path, rel: str, text: str) -> placement.Placement:
    write(root, rel, text)
    tree = ast.parse(text)
    return placement.place(rel, tree, placement.ModuleIndex(root))


@pytest.mark.parametrize(
    ("rel", "text", "module", "name"),
    [
        (
            "base/net/tests/test_relative.py",
            "from ..retry import backoff as call",
            "base.net.retry",
            "call",
        ),
        (
            "base/net/tests/test_relative.py",
            "from .. import retry as subject",
            "base.net.retry",
            "subject",
        ),
        (
            "base/net/tests/nested/test_relative.py",
            "from ...retry import backoff",
            "base.net.retry",
            "backoff",
        ),
        ("base/net/__init__.py", "from .retry import backoff as call", "base.net.retry", "call"),
        (
            "base/net/tests/test_relative.py",
            "from ...db import pool as database",
            "base.db.pool",
            "database",
        ),
        ("base/net/tests/test_relative.py", "from .. import *", "base.net", "*"),
        (
            "base/net/tests/test_relative.py",
            "def run():\n    from ..retry import backoff as call",
            "base.net.retry",
            "call",
        ),
    ],
)
def test_relative_imports_resolve_module_members_and_local_aliases(
    root: pathlib.Path, rel: str, text: str, module: str, name: str
) -> None:
    refs = placement.collect_references(ast.parse(text), placement.ModuleIndex(root), rel)
    assert [(ref.module, ref.names) for ref in refs] == [(module, (name,))]


def test_members_of_one_module_have_one_dependency_with_all_aliases(root: pathlib.Path) -> None:
    text = "from base.net.retry import backoff as call, _sleep as sleep\n"
    refs = placement.collect_references(ast.parse(text), placement.ModuleIndex(root))
    assert [(ref.module, ref.names) for ref in refs] == [("base.net.retry", ("call", "sleep"))]
    write(root, "cli/commands/run.py", text)
    graph = placement.unit_graph(root)
    assert graph.empirical["cli", "base"] == 1


def test_import_bindings_match_python_module_and_member_aliases(root: pathlib.Path) -> None:
    text = (
        "import base.net.retry\nimport base.net.retry as module\n"
        "from base.net import retry as subject\n"
        "from base.net.retry import backoff as call\n"
    )
    clauses = [
        imports.normalize(node, "cli/commands/run.py")
        for node in ast.parse(text).body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert [clause.origins for clause in clauses] == [
        {"base": "base"},
        {"module": "base.net.retry"},
        {"subject": "base.net.retry"},
        {"call": "base.net.retry.backoff"},
    ]
    refs = placement.collect_references(ast.parse(text), placement.ModuleIndex(root))
    assert [ref.module for ref in refs] == ["base.net.retry"] * 4


def test_reexported_member_keeps_its_import_door_as_dependency(root: pathlib.Path) -> None:
    write(root, "base/net/__init__.py", "from .retry import backoff\n")
    tree = ast.parse("from base.net import backoff as call\n")
    refs = placement.collect_references(tree, placement.ModuleIndex(root), "cli/commands/run.py")
    assert [(ref.module, ref.names) for ref in refs] == [("base.net", ("call",))]


def test_relative_module_alias_is_shared_with_patch_and_private_collectors(
    root: pathlib.Path,
) -> None:
    rel = "base/net/tests/test_relative.py"
    text = "from .. import retry as subject\nsubject._sleep()\nmonkeypatch.setattr(subject, '_sleep', None)"
    tree = ast.parse(text)
    points = patch_points.extract_points(list(ast.walk(tree)), rel)
    assert [point.dotted for point in points] == ["base.net.retry._sleep"]
    assert locality.private_imports(tree, rel, ("base",), root) == {}
    assert placement.collect_references(tree, placement.ModuleIndex(root), rel)[0].module == (
        "base.net.retry"
    )


@pytest.mark.parametrize("rel", ["base/__init__.py", "standalone.py", ""])
def test_invalid_relative_import_is_not_unrelated_or_subject_free(
    root: pathlib.Path, rel: str
) -> None:
    tree = ast.parse("from ..base.net import retry\n")
    index = placement.ModuleIndex(root)
    with pytest.raises(imports.InvalidRelativeImportError, match=":1:"):
        placement.collect_references(tree, index, rel)
    with pytest.raises(imports.InvalidRelativeImportError, match=":1:"):
        patch_points.extract_points(list(ast.walk(tree)), rel)
    with pytest.raises(imports.InvalidRelativeImportError, match=":1:"):
        locality.private_imports(tree, rel, ("base",), root)


def test_invalid_source_sample_is_not_an_executed_import(root: pathlib.Path) -> None:
    text = 'SAMPLE = "from ..base.net import retry"\n'
    assert placement.collect_references(ast.parse(text), placement.ModuleIndex(root)) == []


def test_home_is_the_nearest_common_ancestor_of_the_referenced_modules(
    root: pathlib.Path,
) -> None:
    one = _place(root, "tests/test_a.py", "from base.net import retry\n\nretry.backoff()\n")
    assert (one.home, one.unit) == ("base/net", "base")

    two = _place(
        root,
        "tests/test_b.py",
        "from base.net import retry\nfrom base.db import pool\n\nretry.backoff()\npool.acquire()\n",
    )
    assert two.home == "base"


def test_a_tests_package_helper_is_not_evidence_wherever_it_lives(root: pathlib.Path) -> None:
    """A helper under `<pkg>/tests/` is test support, so importing it neither raises nor lowers the home."""
    write(root, "base/db/tests/support.py", "def fake():\n    return None\n")
    found = _place(
        root,
        "tests/test_support.py",
        "from base.db.tests import support\nfrom base.net import retry\n\nsupport.fake()\nretry.backoff()\n",
    )
    assert (found.home, found.unit) == ("base/net", "base")


def test_the_home_unit_is_the_highest_layer_and_only_its_modules_place_the_file(
    root: pathlib.Path,
) -> None:
    found = _place(
        root,
        "tests/test_c.py",
        "from cli.commands import run\nfrom base.net import retry\n\nrun.main()\nretry.backoff()\n",
    )
    assert (found.home, found.unit) == ("cli/commands", "cli")


def test_the_verdict_does_not_depend_on_where_the_file_sits(root: pathlib.Path) -> None:
    text = (
        "from base.net import retry\nfrom base.db import pool\n\nretry.backoff()\npool.acquire()\n"
    )
    top = _place(root, "tests/test_d.py", text)
    moved = _place(root, "base/tests/test_d.py", text)
    deeper = _place(root, "base/net/tests/test_d.py", text)
    assert top == moved == deeper


@pytest.mark.parametrize(
    "rel",
    [
        "tests/e2e/test_full_stack.py",
        "tests/fixtures/db.py",
        "tests/factories/agents.py",
        "tests/conftest.py",
        "conftest.py",
        "tests/integration/test_cluster_instance.py",
    ],
)
def test_top_level_tests_have_no_home(root: pathlib.Path, rel: str) -> None:
    found = _place(root, rel, "from base.net import retry\n\nretry.backoff()\n")
    assert found.home is None


def test_a_file_without_first_party_references_has_no_home(root: pathlib.Path) -> None:
    assert _place(root, "tests/test_e.py", "import json\n\njson.dumps({})\n").home is None


def test_a_string_patch_target_is_not_evidence_of_the_subject(root: pathlib.Path) -> None:
    found = _place(
        root,
        "tests/test_f.py",
        "from base.net import retry\n\n"
        "def test_x(monkeypatch):\n"
        "    retry.backoff()\n"
        "    monkeypatch.setattr('base.db.pool._pool', None)\n",
    )
    assert found.home == "base/net"
    assert not found.fallback


def test_an_import_used_only_as_a_patch_object_is_not_evidence_of_the_subject(
    root: pathlib.Path,
) -> None:
    only_patched = _place(
        root,
        "tests/test_g.py",
        "from base.net import retry\nfrom base.db import pool\n\n"
        "def test_x(monkeypatch):\n"
        "    retry.backoff()\n"
        "    monkeypatch.setattr(pool, '_pool', None)\n",
    )
    assert only_patched.home == "base/net"

    also_used = _place(
        root,
        "tests/test_h.py",
        "from base.net import retry\nfrom base.db import pool\n\n"
        "def test_x(monkeypatch):\n"
        "    retry.backoff()\n"
        "    pool.acquire()\n"
        "    monkeypatch.setattr(pool, '_pool', None)\n",
    )
    assert also_used.home == "base"


def test_patch_object_and_mock_patch_object_are_patch_evidence_too(root: pathlib.Path) -> None:
    found = _place(
        root,
        "tests/test_i.py",
        "from unittest import mock\nfrom base.net import retry\nfrom base.db import pool\n\n"
        "def test_x():\n"
        "    retry.backoff()\n"
        "    with mock.patch.object(pool, '_pool', None):\n"
        "        pass\n",
    )
    assert found.home == "base/net"


def test_when_every_strong_reference_is_patch_evidence_nothing_is_dropped(
    root: pathlib.Path,
) -> None:
    """The documented fallback: such a file keeps the home its patch targets give it."""
    found = _place(
        root,
        "tests/test_j.py",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', print)\n",
    )
    assert found.home == "base/net"
    assert found.fallback

    imported = _place(
        root,
        "tests/test_k.py",
        "from base.db import pool\n\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setattr(pool, '_pool', None)\n",
    )
    assert (imported.home, imported.fallback) == ("base/db", True)


def test_a_pair_the_contracts_are_silent_about_follows_the_source_import_direction(
    root: pathlib.Path,
) -> None:
    make_repo(
        root,
        {
            "services/__init__.py": "",
            "services/worker/__init__.py": "",
            "services/worker/run.py": "from base.net import retry\n\nretry.backoff()\n",
        },
    )
    found = _place(
        root,
        "tests/test_l.py",
        "from services.worker import run\nfrom base.net import retry\n\nrun\nretry.backoff()\n",
    )
    assert (found.home, found.unit) == ("services/worker", "services.worker")


def test_evidence_that_is_not_an_import_still_places_the_file(root: pathlib.Path) -> None:
    by_climb = _place(
        root,
        "tests/test_m.py",
        "import pathlib\n\nROOT = pathlib.Path(__file__).resolve().parents[1]\n"
        "SOURCE = ROOT / 'base' / 'net' / 'retry.py'\n",
    )
    assert by_climb.home == "base/net"

    embedded = _place(
        root,
        "tests/test_n.py",
        "import subprocess\nimport sys\n\nSNIPPET = '''\nfrom base.db import pool\npool.acquire()\n'''\n\n"
        "def test_x():\n    subprocess.run([sys.executable, '-c', SNIPPET])\n",
    )
    assert embedded.home == "base/db"


@pytest.mark.parametrize(
    ("rel", "root_expression"),
    [
        ("tests/test_o.py", "pathlib.Path(__file__).resolve().parents[1]"),
        ("tests/sub/test_o.py", "pathlib.Path(__file__).resolve().parents[2]"),
        ("tests/sub/test_o.py", "pathlib.Path(__file__).parent.parent.parent"),
        ("test_o.py", "pathlib.Path(__file__).parent"),
        ("tests/test_o.py", "repo_root()"),
    ],
)
def test_a_path_chain_from_the_repository_root_is_evidence(
    root: pathlib.Path, rel: str, root_expression: str
) -> None:
    found = _place(
        root,
        rel,
        f"import pathlib\n\nSOURCE = {root_expression} / 'base' / 'net' / 'retry.py'\n",
    )
    assert found.home == "base/net"


def test_a_name_bound_only_to_the_repository_root_is_the_root_in_any_scope(
    root: pathlib.Path,
) -> None:
    found = _place(
        root,
        "tests/test_p.py",
        "import pathlib\n\nHERE = pathlib.Path(__file__).resolve().parents[1]\nROOT = HERE\n\n"
        "def test_x():\n    checkout = ROOT\n    (checkout / 'base' / 'net' / 'retry.py').read_text()\n",
    )
    assert found.home == "base/net"


@pytest.mark.parametrize(
    "anchor",
    [
        "tmp_path",  # a sample tree
        "pathlib.Path(__file__).parent",  # the directory beside the test
        "pathlib.Path(__file__).resolve().parents[0]",  # one short of the root
        "pathlib.Path(__file__).resolve().parents[2]",  # past the root
        "pathlib.Path('.')",  # no declared root
        "repo_root(1)",
    ],
)
def test_a_path_chain_not_rooted_at_the_repository_root_is_data(
    root: pathlib.Path, anchor: str
) -> None:
    found = _place(
        root,
        "tests/test_q.py",
        f"import pathlib\n\nSAMPLE = {anchor} / 'base' / 'net' / 'retry.py'\n",
    )
    assert found.home is None


def test_a_chain_must_begin_with_a_code_directory_right_after_the_root(root: pathlib.Path) -> None:
    found = _place(
        root,
        "tests/test_r.py",
        "import pathlib\n\nROOT = pathlib.Path(__file__).resolve().parents[1]\n"
        "FIXTURE = ROOT / 'tests' / 'base' / 'net' / 'retry.py'\n",
    )
    assert found.home is None


@pytest.mark.parametrize(
    "body",
    [
        # bound to a sample tree in one test and to the root in another
        "def test_a(tmp_path):\n    repo = tmp_path / 'repo'\n"
        "    (repo / 'base' / 'net' / 'retry.py').write_text('')\n\n"
        "def test_b():\n    repo = pathlib.Path(__file__).resolve().parents[1]\n"
        "    (repo / 'base' / 'net' / 'retry.py').read_text()\n",
        # a parameter of the same name is not the module constant
        "ROOT = pathlib.Path(__file__).resolve().parents[1]\n\n"
        "def test_a(ROOT):\n    (ROOT / 'base' / 'net' / 'retry.py').write_text('')\n",
    ],
)
def test_a_name_with_another_binding_is_not_the_root(root: pathlib.Path, body: str) -> None:
    found = _place(root, "tests/test_s.py", f"import pathlib\n\n{body}")
    assert found.home is None


def test_a_bare_path_string_names_no_root(root: pathlib.Path) -> None:
    found = _place(
        root,
        "tests/test_t.py",
        "import pytest\n\n"
        "@pytest.mark.parametrize('path', ['base/net/retry.py', 'cli/commands/run.py'])\n"
        "def test_x(path):\n    assert path\n",
    )
    assert found.home is None


def test_source_in_a_string_is_evidence_only_where_the_file_runs_it(root: pathlib.Path) -> None:
    sample = "SNIPPET = '''\nfrom base.db import pool\npool.acquire()\n'''\n"
    handed_to_a_linter = _place(
        root, "tests/test_u.py", f"{sample}\ndef test_x():\n    lint(SNIPPET)\n"
    )
    assert handed_to_a_linter.home is None

    run = _place(
        root,
        "tests/test_v.py",
        f"import subprocess\nimport sys\n\n{sample}\n"
        "def test_x():\n    subprocess.run([sys.executable, '-c', SNIPPET])\n",
    )
    assert run.home == "base/db"


def test_a_missing_services_directory_is_a_legal_repository(root: pathlib.Path) -> None:
    assert not (root / "services").exists()
    assert "services" in placement.unit_graph(root).units


def test_units_no_contract_or_source_edge_relates_are_ambiguous_and_the_filename_decides(
    root: pathlib.Path,
) -> None:
    make_repo(
        root,
        {
            "services/__init__.py": "",
            "services/alpha/__init__.py": "",
            "services/alpha/run.py": "def go():\n    pass\n",
            "services/beta/__init__.py": "",
            "services/beta/tool.py": "def use():\n    pass\n",
        },
    )
    both = (
        "from services.alpha import run\nfrom services.beta import tool\n\nrun.go()\ntool.use()\n"
    )
    named = _place(root, "tests/test_tool.py", both)
    assert (named.home, named.unit, named.ambiguous) == ("services/beta", "services.beta", True)
    other = _place(root, "tests/test_run_alpha.py", both)
    assert (other.home, other.unit, other.ambiguous) == ("services/alpha", "services.alpha", True)


@pytest.mark.parametrize("top", ["schedules", "commands", "demos"])
def test_runnable_template_paths_have_their_code_owner(root: pathlib.Path, top: str) -> None:
    write(root, f"{top}/daily-run.py", "from base.net import retry\nretry.backoff()\n")
    text = (
        "from pathlib import Path\nfrom base.net import retry\n"
        f"SOURCE = Path(__file__).resolve().parents[1] / '{top}' / 'daily-run.py'\n"
        "retry.backoff()\n"
    )
    found = _place(root, "tests/test_template.py", text)
    assert (found.home, found.unit, found.ambiguous) == (top, top, False)


@pytest.mark.parametrize("top", ["schedules", "commands", "demos"])
def test_runnable_imports_have_the_same_home_after_test_moves(root: pathlib.Path, top: str) -> None:
    write(root, f"{top}/run.py", "def main():\n    return None\n")
    text = f"from {top} import run\nrun.main()\n"
    before = _place(root, "tests/test_template.py", text)
    after = _place(root, f"{top}/tests/test_template.py", text)
    assert before == after
    assert (before.home, before.unit) == (top, top)


@pytest.mark.parametrize("top", ["schedules", "commands", "demos"])
def test_runnable_tests_do_not_gain_access_to_dependency_private_names(
    root: pathlib.Path, top: str
) -> None:
    write(root, f"{top}/run.py", "from base.net import retry\n\ndef main():\n    retry.backoff()\n")
    text = (
        f"from {top} import run\nfrom base.net import retry\n"
        "def test_run(monkeypatch):\n"
        "    monkeypatch.setattr(retry, '_sleep', lambda seconds: None)\n"
        "    run.main()\n"
    )
    rel = f"{top}/tests/test_run.py"
    result = patch_targets.analyze(rel, text, patch_targets.Classifier(placement.ModuleIndex(root)))
    assert result.home == top
    assert patch_targets.violations(rel, result) == {f"{rel}::base.net.retry._sleep": [4]}
