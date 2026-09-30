"""The placement rule: a test's home comes from its own first-party references."""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import locality, placement
from tests.scripts.structure.patch_repo import make_repo, write


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    locality.reset_caches()
    return make_repo(tmp_path)


def _place(root: pathlib.Path, rel: str, text: str) -> placement.Placement:
    write(root, rel, text)
    tree = ast.parse(text)
    return placement.place(rel, tree, placement.ModuleIndex(root))


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
    by_path = _place(
        root,
        "tests/test_m.py",
        "import pathlib\n\nSOURCE = pathlib.Path('.') / 'base' / 'net' / 'retry.py'\n",
    )
    assert by_path.home == "base/net"

    embedded = _place(
        root,
        "tests/test_n.py",
        "SNIPPET = '''\nfrom base.db import pool\npool.acquire()\n'''\n",
    )
    assert embedded.home == "base/db"


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
