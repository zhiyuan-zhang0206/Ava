from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, run

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "COUNT = 0\n\ndef helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
}


def patches(make_repo: RepoFactory, source: str) -> list[tuple[str, str]]:
    root = make_repo({**LIB, "app/test_x.py": source})
    return sorted(
        (d.code.value, d.debt.key)
        for d in analysis_of(root).diagnostics
        if d.debt is not None and d.path == "app/test_x.py" and d.code.value.endswith("-patch")
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from lib import mod\n\ndef test_a():\n    mod.helper = None\n",
            [("first-party-patch", "test_x.py#assignment lib.mod.helper")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    mod.COUNT += 1\n",
            [("first-party-patch", "test_x.py#assignment lib.mod.COUNT")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    mod.COUNT: int = 3\n",
            [("first-party-patch", "test_x.py#assignment lib.mod.COUNT")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    del mod.helper\n",
            [("first-party-patch", "test_x.py#del lib.mod.helper")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    mod.helper, mod.COUNT = None, 1\n",
            [
                ("first-party-patch", "test_x.py#assignment lib.mod.COUNT"),
                ("first-party-patch", "test_x.py#assignment lib.mod.helper"),
            ],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    setattr(mod, 'helper', None)\n",
            [("first-party-patch", "test_x.py#setattr lib.mod.helper")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    delattr(mod, 'helper')\n",
            [("first-party-patch", "test_x.py#delattr lib.mod.helper")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    mod.__dict__['helper'] = None\n",
            [("first-party-patch", "test_x.py#__dict__ write lib.mod.helper")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    vars(mod)['helper'] = None\n",
            [("first-party-patch", "test_x.py#__dict__ write lib.mod.helper")],
        ),
        (
            "import time\n\ndef test_a():\n    time.sleep = None\n",
            [("stdlib-patch", "test_x.py#assignment time.sleep")],
        ),
        (
            "import requests\n\ndef test_a():\n    requests.get = None\n",
            [("third-party-patch", "test_x.py#assignment requests.get")],
        ),
        (
            "from lib import mod\n\ndef test_a():\n    mod.helper.cache = {}\n",
            [("first-party-patch", "test_x.py#assignment lib.mod.helper.cache")],
        ),
    ],
)
def test_each_form_of_attribute_write_is_a_patch(
    make_repo: RepoFactory, source: str, expected: list[tuple[str, str]]
) -> None:
    assert patches(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "def test_a(thing):\n    thing.x = 1\n",
        "from lib import mod\n\ndef test_a(mod):\n    mod.helper = 1\n",
        "class Local:\n    go = None\n\ndef test_a():\n    Local.go = 1\n",
        "class Box:\n    def __init__(self):\n        self.x = 1\n",
        "from lib import mod\n\nCOUNT = mod.COUNT\n\ndef test_a():\n    global COUNT\n    COUNT = 2\n",
        "from lib import mod\n\ndef test_a():\n    mod.helper()\n    value = mod.COUNT\n    return value\n",
    ],
)
def test_a_write_that_reaches_no_imported_name_is_not_a_patch(
    make_repo: RepoFactory, source: str
) -> None:
    assert patches(make_repo, source) == []


def test_a_module_writing_to_itself_is_not_a_patch(make_repo: RepoFactory) -> None:
    root = make_repo({**LIB, "lib/own.py": "import lib.own as me\n\nX = 1\nme.X = 2\n"})
    assert [d for d in analysis_of(root).diagnostics if d.code.value.endswith("-patch")] == []


def test_an_attribute_write_is_recorded_like_any_other_patch(make_repo: RepoFactory) -> None:
    source = "from lib import mod\n\ndef test_a():\n    mod.helper = None\n"
    root = make_repo({**LIB, "app/test_x.py": source})
    assert run(root, "debt", "record", "app") == 0
    assert "[debt.first-party-patch]" in (root / "app/meizel.toml").read_text()


@pytest.mark.parametrize(
    "source",
    [
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'nope', 1, raising=False)\n",
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.delattr(mod, 'nope', raising=False)\n",
        "from lib import mod\n\ndef test_a():\n    mod.nope = 1\n",
        "from lib import mod\n\ndef test_a():\n    mod.nope += 1\n",
        "from lib import mod\n\ndef test_a():\n    del mod.nope\n",
        "from lib import mod\n\ndef test_a():\n    setattr(mod, 'nope', 1)\n",
        "from unittest import mock\nfrom lib import mod\n\ndef test_a():\n    with mock.patch.object(mod, 'nope'):\n        pass\n",
    ],
)
def test_a_name_the_module_does_not_bind_is_an_error_not_debt(
    make_repo: RepoFactory, source: str
) -> None:
    root = make_repo({**LIB, "app/test_x.py": source})
    [found] = diagnostics(root, "missing-patch-target")
    assert found.debt is None
    assert "lib/mod.py binds no `nope`" in found.message
    run(root, "debt", "record", "app")
    assert len(diagnostics(root, "missing-patch-target")) == 1


MODULES = {
    "bound by def": "def target(): ...\n",
    "bound by class": "class target: ...\n",
    "bound by assignment": "target = 1\n",
    "bound by annotation with a value": "target: int = 1\n",
    "bound by import": "import target\n",
    "bound by import as": "import other as target\n",
    "bound by from-import": "from other import target\n",
    "bound by from-import as": "from other import thing as target\n",
    "bound in a conditional": "if True:\n    target = 1\n",
    "bound in a try": "try:\n    import target\nexcept ImportError:\n    target = None\n",
    "bound by a loop": "for target in range(2):\n    pass\n",
    "bound by with": "with open('f') as target:\n    pass\n",
    "bound by walrus": "if (target := 1):\n    pass\n",
    "bound by except": "try:\n    pass\nexcept Exception as target:\n    pass\n",
    "bound by global in a function": "def init():\n    global target\n    target = 1\n",
    "served by __getattr__": "def __getattr__(name):\n    return name\n",
    "served by a star import": "from other import *\n",
    "served by globals()": "globals().update(x=1)\n",
}


@pytest.mark.parametrize("body", [pytest.param(b, id=k) for k, b in MODULES.items()])
def test_a_name_the_module_binds_or_serves_is_not_missing(
    make_repo: RepoFactory, body: str
) -> None:
    source = "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'target', 1)\n"
    root = make_repo({**LIB, "lib/mod.py": body, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []


def test_a_builtin_name_is_bound_because_the_module_resolves_it(make_repo: RepoFactory) -> None:
    source = (
        "from lib import mod\n\ndef test_a(monkeypatch):\n"
        "    monkeypatch.setattr(mod, 'print', len, raising=False)\n"
    )
    root = make_repo({**LIB, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []
    assert len(diagnostics(root, "first-party-patch")) == 1


def test_a_submodule_of_a_package_is_bound(make_repo: RepoFactory) -> None:
    source = "import lib\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(lib, 'mod', None)\n"
    root = make_repo({**LIB, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []


def test_a_package_has_a_path(make_repo: RepoFactory) -> None:
    source = (
        "import lib\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(lib, '__path__', [])\n"
    )
    root = make_repo({**LIB, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []


def test_only_a_first_party_module_is_checked_for_the_name(make_repo: RepoFactory) -> None:
    source = (
        "import time\nimport requests\n\ndef test_a(monkeypatch):\n"
        "    monkeypatch.setattr(time, 'nope', 1, raising=False)\n"
        "    requests.nope = 1\n"
    )
    root = make_repo({**LIB, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []


def test_an_object_reached_through_a_module_is_not_checked_for_the_name(
    make_repo: RepoFactory,
) -> None:
    source = "from lib import mod\n\ndef test_a():\n    mod.helper.nope = 1\n"
    root = make_repo({**LIB, "app/test_x.py": source})
    assert diagnostics(root, "missing-patch-target") == []
