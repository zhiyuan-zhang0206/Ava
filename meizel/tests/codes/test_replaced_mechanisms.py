from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures

PACKAGES = ["base", "meiva", "agent", "gateway", "cli", "ops", "services", "meiva_builtins"]
STACK = (
    '[uses]\n"meiva" = ["base"]\n"agent" = ["meiva", "base"]\n"gateway" = ["agent", "meiva", "base", "ops"]\n'
    '"cli" = ["gateway", "agent", "meiva", "base", "ops", "services"]\n"ops" = ["base"]\n'
    '"services" = ["base", "ops"]\n"meiva_builtins" = ["base", "meiva", "agent"]\n'
)


def layered(make_repo: RepoFactory, importer: str, imported: str) -> list[tuple[str, str]]:
    files = {f"{p}/__init__.py": "" for p in PACKAGES}
    files |= {f"{p}/api.py": "VALUE = 1\n" for p in PACKAGES}
    files |= {f"{p}/meizel.toml": 'exports = ["api.py"]\n' for p in PACKAGES}
    files[f"{importer}/uses.py"] = f"from {imported} import api\n"
    files["meizel.toml"] = STACK.replace(f'"{importer}" = [', f'"{importer}" = [', 1)
    root = make_repo(files)
    return sorted(
        (d.code.value, d.path) for d in analysis_of(root).verdict if d.path.endswith("uses.py")
    )


@pytest.mark.parametrize(
    ("importer", "imported"),
    [
        ("base", "meiva"),
        ("base", "agent"),
        ("meiva", "agent"),
        ("agent", "gateway"),
        ("gateway", "cli"),
        ("base", "meiva_builtins"),
        ("base", "services"),
        ("services", "cli"),
        ("ops", "gateway"),
        ("ops", "cli"),
        ("base", "ops"),
        ("gateway", "services"),
    ],
)
def test_an_import_against_the_layering_is_an_undeclared_edge(
    make_repo: RepoFactory, importer: str, imported: str
) -> None:
    assert layered(make_repo, importer, imported) == [("undeclared-edge", f"{importer}/uses.py")]


@pytest.mark.parametrize(
    ("importer", "imported"), [("cli", "base"), ("agent", "meiva"), ("gateway", "ops")]
)
def test_an_import_down_the_stack_passes(
    make_repo: RepoFactory, importer: str, imported: str
) -> None:
    assert layered(make_repo, importer, imported) == []


def test_declaring_the_edge_the_layering_refuses_closes_a_cycle(make_repo: RepoFactory) -> None:
    files = {
        "base/__init__.py": "",
        "base/api.py": "from agent import api\n",
        "agent/__init__.py": "",
        "agent/api.py": "from base import api\n",
        "meizel.toml": '[uses]\n"base" = ["agent"]\n"agent" = ["base"]\n',
    }
    [found] = diagnostics(make_repo(files), "manifest-cycle")
    assert "base -> agent -> base" in found.message or "agent -> base -> agent" in found.message


DOORS = {
    "a/__init__.py": "",
    "a/_impl.py": "VALUE = 1\n",
    "a/api.py": "from a import _impl\n_secret = 2\nPUBLIC = 3\n",
    "a/meizel.toml": 'exports = ["api.py"]\n\n[uses]\n"api.py" = ["_impl.py"]\n',
    "b/__init__.py": "",
    "meizel.toml": '[uses]\n"b" = ["a"]\n',
}


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("from a.api import _secret\n", "private-name"),
        ("from a import api\n\nvalue = api._secret\n", "private-name"),
        ("import a.api as module\n\nvalue = module._secret\n", "private-name"),
        ("from a import _impl\n", "private-module"),
        ("import a._impl\n", "private-module"),
    ],
)
def test_reaching_a_private_name_or_module_of_another_package_is_reported(
    make_repo: RepoFactory, source: str, code: str
) -> None:
    root = make_repo({**DOORS, "b/use.py": source})
    assert code in {c for c, path in failures(root) if path == "b/use.py"}


def test_a_public_name_through_an_exported_door_passes(make_repo: RepoFactory) -> None:
    root = make_repo({**DOORS, "b/use.py": "from a.api import PUBLIC\n"})
    assert [p for _, p in failures(root) if p == "b/use.py"] == []


def test_a_sibling_may_use_an_underscore_module_of_its_own_package(make_repo: RepoFactory) -> None:
    assert [p for _, p in failures(make_repo(DOORS)) if p.startswith("a/")] == []


POOL = {
    "base/__init__.py": "",
    "base/db/__init__.py": "",
    "base/db/pool.py": "_pool = None\n\n\ndef acquire():\n    return _pool\n",
    "base/db/meizel.toml": 'exports = ["pool.py"]\n',
    "app/__init__.py": "",
    "meizel.toml": '[uses]\n"app" = ["base"]\n',
}


@pytest.mark.parametrize(
    ("source", "codes"),
    [
        (
            "from base.db import pool\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(pool, '_pool', None)\n",
            {"private-name", "first-party-patch"},
        ),
        (
            "from unittest import mock\nfrom base.db import pool\n\ndef test_a():\n    with mock.patch.object(pool, '_pool'):\n        pass\n",
            {"private-name", "first-party-patch"},
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setattr('base.db.pool._pool', None)\n",
            {"restricted-python"},
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch('base.db.pool._pool'):\n        pass\n",
            {"restricted-python"},
        ),
    ],
)
def test_a_patch_of_another_packages_private_name_is_reported(
    make_repo: RepoFactory, source: str, codes: set[str]
) -> None:
    root = make_repo({**POOL, "app/test_x.py": source})
    assert codes <= {d.code.value for d in analysis_of(root).verdict if d.path == "app/test_x.py"}


def test_entering_a_component_through_a_module_it_does_not_export_is_reported(
    make_repo: RepoFactory,
) -> None:
    files = {
        "comp/__init__.py": "from comp.api import PUBLIC\n",
        "comp/api.py": "PUBLIC = 1\n",
        "comp/internal.py": "HIDDEN = 2\n",
        "comp/meizel.toml": 'exports = ["api.py"]\n\n[uses]\n"__init__.py" = ["api.py"]\n',
        "user/__init__.py": "",
        "user/a.py": "from comp import internal\n",
        "user/b.py": "from comp import api\n",
        "meizel.toml": '[uses]\n"user" = ["comp"]\n',
    }
    found = failures(make_repo(files))
    assert ("not-exported", "user/a.py") in found
    assert ("not-exported", "user/b.py") not in found
