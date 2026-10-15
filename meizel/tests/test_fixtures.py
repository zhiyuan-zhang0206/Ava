from __future__ import annotations

from pathlib import Path

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, edges, verdict


def fixture_edges(root: Path) -> set[tuple[str, str]]:
    return {(s, d) for s, d, k in edges(root) if k == "fixture"}


DEFINE = "import pytest\n\n@pytest.fixture\ndef {name}():\n    return 1\n"


def test_a_test_resolves_a_fixture_to_the_nearest_conftest_above_it(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/test_a.py": "def test_a(shared):\n    pass\n",
            "other/tests/test_b.py": "def test_b(shared):\n    pass\n",
        }
    )
    assert fixture_edges(root) == {
        ("pkg/tests/test_a.py", "pkg/tests/conftest.py"),
        ("other/tests/test_b.py", "conftest.py"),
    }


def test_ancestor_conftests_need_no_declaration(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/tests/test_a.py": "def test_a(shared):\n    pass\n",
        }
    )
    assert verdict(root) == set()


def test_a_fixture_defined_in_the_test_module_is_local(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": DEFINE.format(name="shared") + "\ndef test_a(shared):\n    pass\n",
        }
    )
    assert fixture_edges(root) == set()


def test_an_overriding_fixture_that_requests_its_own_name_reaches_the_parent(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "pkg/conftest.py": "import pytest\n\n@pytest.fixture\ndef shared(shared):\n    return shared\n",
        }
    )
    assert fixture_edges(root) == {("pkg/conftest.py", "conftest.py")}


def test_a_conftest_in_another_branch_is_not_visible(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "a/conftest.py": DEFINE.format(name="only_a"),
            "b/tests/test_b.py": "def test_b(only_a):\n    pass\n",
        }
    )
    assert fixture_edges(root) == set()


def test_parametrized_names_defaults_and_helpers_are_not_requests(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.parametrize('shared', [1])\n"
                "def test_a(shared):\n    pass\n\n"
                "def test_b(shared=3):\n    pass\n\n"
                "def helper(shared):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == set()


def test_indirect_parametrization_requests_the_fixture(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.parametrize('shared', [1], indirect=True)\n"
                "def test_a(shared):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == {("t/test_a.py", "conftest.py")}


def test_usefixtures_and_getfixturevalue_request_by_literal_name(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="one")
            + DEFINE.format(name="two").replace("import pytest\n\n", ""),
            "t/test_a.py": (
                "import pytest\n\n"
                "@pytest.mark.usefixtures('one')\n"
                "def test_a(request):\n    request.getfixturevalue('two')\n"
            ),
        }
    )
    found = {f.names for f in analysis_of(root).facts if f.kind.value == "fixture"}
    assert found == {("one",), ("two",)}


def test_mock_arguments_injected_by_patch_decorators_are_not_requests(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="mocked"),
            "lib/__init__.py": "",
            "lib/m.py": "x = 1\n",
            "t/test_a.py": (
                "from unittest import mock\nfrom lib import m\n\n"
                "@mock.patch.object(m, 'x')\n"
                "def test_a(mocked):\n    pass\n"
            ),
        }
    )
    assert fixture_edges(root) == set()


def test_class_methods_skip_self(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="shared"),
            "t/test_a.py": "class TestK:\n    def test_a(self, shared):\n        pass\n",
        }
    )
    assert fixture_edges(root) == {("t/test_a.py", "conftest.py")}


def plugin_repo(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "tests/__init__.py": "",
        "tests/fixtures/__init__.py": "",
        "tests/fixtures/db.py": DEFINE.format(name="db"),
        "conftest.py": "pytest_plugins = ['tests.fixtures.db']\n",
        "base/x/tests/test_a.py": "def test_a(db):\n    pass\n",
        **(extra or {}),
    }


def test_a_literal_pytest_plugins_module_defines_fixtures_for_everything_below(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(plugin_repo())
    assert fixture_edges(root) == {("base/x/tests/test_a.py", "tests/fixtures/db.py")}
    assert ("conftest.py", "tests/fixtures/db.py", "plugin") in edges(root)


def test_plugin_fixtures_are_ordinary_edges_that_must_be_declared_and_exported(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(plugin_repo())
    found = {(d.code.value, d.path) for d in diagnostics(root, "undeclared-edge")}
    assert found == {
        ("undeclared-edge", "conftest.py"),
        ("undeclared-edge", "base/x/tests/test_a.py"),
    }


def test_declared_plugin_edges_pass(make_repo: RepoFactory) -> None:
    root = make_repo(
        plugin_repo(
            {
                "meizel.toml": '[uses]\n"conftest.py" = ["tests"]\n"base" = ["tests"]\n',
                "tests/meizel.toml": 'exports = ["fixtures"]\n',
                "tests/fixtures/meizel.toml": 'exports = ["db.py"]\n',
            }
        )
    )
    assert verdict(root) == set()


def test_requesting_a_private_fixture_name_is_a_private_use(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "conftest.py": DEFINE.format(name="_secret"),
            "t/test_a.py": "def test_a(_secret):\n    pass\n",
        }
    )
    [found] = diagnostics(root, "private-name")
    assert found.path == "t/test_a.py"


def test_an_underscore_fixture_works_through_autouse_and_in_its_own_file(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "conftest.py": (
                "import pytest\n\n@pytest.fixture(autouse=True)\ndef _quiet():\n    return 1\n"
            ),
            "t/test_a.py": (
                "import pytest\n\n@pytest.fixture\ndef _local():\n    return 1\n\n"
                "def test_a(_local):\n    pass\n"
            ),
        }
    )
    assert verdict(root) == set()
