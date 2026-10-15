from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, git, run

LIB = {"lib/__init__.py": "", "lib/mod.py": "def helper() -> int:\n    return 1\n"}


def codes(make_repo: RepoFactory, source: str) -> list[tuple[str, str]]:
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    return sorted(
        (d.code.value, d.debt.key)
        for d in analysis_of(root).diagnostics
        if d.debt is not None and d.path == "app/test_x.py" and d.code.value.endswith("patch")
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', None)\n",
            ("stdlib-patch", "test_x.py#monkeypatch.setattr time.sleep"),
        ),
        (
            "import json\nfrom unittest import mock\n\ndef test_a():\n    with mock.patch.object(json, 'dumps'):\n        pass\n",
            ("stdlib-patch", "test_x.py#patch.object json.dumps"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch('time.sleep'):\n        pass\n",
            ("stdlib-patch", "test_x.py#patch time.sleep"),
        ),
        (
            "import requests\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(requests, 'get', None)\n",
            ("third-party-patch", "test_x.py#monkeypatch.setattr requests.get"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch('requests.get'):\n        pass\n",
            ("third-party-patch", "test_x.py#patch requests.get"),
        ),
        (
            "import os\nfrom unittest import mock\n\ndef test_a():\n    with mock.patch.dict(os.environ, {'A': '1'}):\n        pass\n",
            ("env-patch", "test_x.py#patch.dict os.environ"),
        ),
        (
            "from unittest import mock\n\ndef test_a():\n    with mock.patch.dict('os.environ', {'A': '1'}):\n        pass\n",
            ("env-patch", "test_x.py#patch.dict os.environ"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setenv('A', '1')\n",
            ("env-patch", "test_x.py#monkeypatch.setenv os.environ.A"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.delenv('A', raising=False)\n",
            ("env-patch", "test_x.py#monkeypatch.delenv os.environ.A"),
        ),
        (
            "import os\n\ndef test_a(monkeypatch):\n    monkeypatch.setitem(os.environ, 'A', '1')\n",
            ("env-patch", "test_x.py#monkeypatch.setitem os.environ.A"),
        ),
        (
            "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1)\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr thing.x"),
        ),
        (
            "class Local:\n    def go(self): ...\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(Local, 'go', None)\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr Local.go"),
        ),
        (
            "def test_a(monkeypatch):\n    monkeypatch.setattr(make().inner, 'x', 1)\n\ndef make(): ...\n",
            ("unresolved-patch", "test_x.py#monkeypatch.setattr make().inner.x"),
        ),
        (
            "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None)\n",
            ("first-party-patch", "test_x.py#monkeypatch.setattr lib.mod.helper"),
        ),
    ],
)
def test_each_kind_of_patch_has_its_own_code(
    make_repo: RepoFactory, source: str, expected: tuple[str, str]
) -> None:
    assert codes(make_repo, source) == [expected]


def test_a_first_party_string_target_stays_restricted_python(make_repo: RepoFactory) -> None:
    source = "from unittest import mock\n\ndef test_a():\n    with mock.patch('lib.mod.helper'):\n        pass\n"
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert [d for d in analysis_of(root).diagnostics if d.code.value.endswith("patch")] == []
    assert len(diagnostics(root, "restricted-python")) == 1


def test_an_unreadable_string_target_stays_restricted_python(make_repo: RepoFactory) -> None:
    source = (
        "from unittest import mock\n\ndef test_a(name):\n    with mock.patch(name):\n        pass\n"
    )
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert len(diagnostics(root, "restricted-python")) == 1


def test_every_patch_code_is_recorded_like_any_other_debt(make_repo: RepoFactory) -> None:
    source = (
        "import time\n\ndef test_a(monkeypatch, thing):\n"
        "    monkeypatch.setattr(time, 'sleep', None)\n"
        "    monkeypatch.setattr(thing, 'x', 1)\n"
        "    monkeypatch.setenv('A', '1')\n"
    )
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert run(root, "debt", "record", "app") == 0
    git(root, "add", "-A")
    text = (root / "app/meizel.toml").read_text()
    assert "[debt.env-patch]" in text
    assert "[debt.stdlib-patch]" in text
    assert "[debt.unresolved-patch]" in text
    assert [d for d in analysis_of(root).verdict if d.code.value.endswith("patch")] == []


def test_the_messages_say_what_to_do_instead(make_repo: RepoFactory) -> None:
    source = "import time\n\ndef test_a(monkeypatch, thing):\n    monkeypatch.setattr(time, 'sleep', None)\n    monkeypatch.setattr(thing, 'x', 1)\n    monkeypatch.setenv('A', '1')\n"
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    texts = {
        d.code.value: d.render()
        for d in analysis_of(root).diagnostics
        if d.code.value.endswith("patch")
    }
    assert "explicit parameter" in texts["stdlib-patch"]
    assert "read the setting once at the boundary" in texts["env-patch"]
    assert "not an imported module or object" in texts["unresolved-patch"]


def test_a_new_unresolved_patch_in_a_strict_to_be_directory_fails(make_repo: RepoFactory) -> None:
    source = "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1)\n    monkeypatch.setattr(thing, 'y', 1)\n"
    baseline = '[debt.unresolved-patch]\n"test_x.py#monkeypatch.setattr thing.x" = 1\n'
    root = make_repo(
        {**LIB, "app/__init__.py": "", "app/test_x.py": source, "app/meizel.toml": baseline}
    )
    [found] = [d for d in analysis_of(root).verdict if d.code.value == "unresolved-patch"]
    assert "thing.y" in found.message
    assert "app is strict-to-be" in found.render()
