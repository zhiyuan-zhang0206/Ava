"""Patching first-party code is a diagnostic of its own: the dependency was not injected."""

from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, git, run

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "REGISTRY = {}\n\nclass Client:\n    def send(self) -> None: ...\n\ndef helper() -> int:\n    return 1\n",
    "lib/other.py": "",
}


def patched(make_repo: RepoFactory, source: str, extra: dict[str, str] | None = None):
    root = make_repo({**LIB, **(extra or {}), "app/__init__.py": "", "app/test_x.py": source})
    return [d for d in diagnostics(root, "first-party-patch") if d.path == "app/test_x.py"]


@pytest.mark.parametrize(
    ("source", "key"),
    [
        (
            "from unittest import mock\nfrom lib import mod\n\ndef test_a():\n    with mock.patch.object(mod, 'helper'):\n        pass\n",
            "test_x.py#patch.object lib.mod.helper",
        ),
        (
            "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', lambda: 2)\n",
            "test_x.py#monkeypatch.setattr lib.mod.helper",
        ),
        (
            "import lib.mod\n\ndef test_a(monkeypatch):\n    monkeypatch.delattr(lib.mod, 'helper')\n",
            "test_x.py#monkeypatch.delattr lib.mod.helper",
        ),
        (
            "from lib import mod as m\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(m, 'helper', None)\n",
            "test_x.py#monkeypatch.setattr lib.mod.helper",
        ),
        (
            "from lib.mod import Client\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(Client, 'send', lambda self: None)\n",
            "test_x.py#monkeypatch.setattr lib.mod.Client.send",
        ),
        (
            "from unittest.mock import patch\nfrom lib import mod\n\n@patch.object(mod.Client, 'send')\ndef test_a(send):\n    pass\n",
            "test_x.py#patch.object lib.mod.Client.send",
        ),
        (
            "from unittest.mock import patch\nfrom lib import mod\n\ndef test_a():\n    with patch.dict(mod.REGISTRY, {'a': 1}):\n        pass\n",
            "test_x.py#patch.dict lib.mod.REGISTRY",
        ),
    ],
)
def test_patching_first_party_code_is_reported_with_a_line_free_key(
    make_repo: RepoFactory, source: str, key: str
) -> None:
    [found] = patched(make_repo, source)
    assert found.debt is not None
    assert (found.debt.owner, found.debt.key) == ("app", key)
    assert found.line >= 4


def test_every_name_of_patch_multiple_is_its_own_site(make_repo: RepoFactory) -> None:
    source = (
        "from unittest.mock import patch\nfrom lib import mod\n\n"
        "def test_a():\n    with patch.multiple(mod, helper=1, REGISTRY={}):\n        pass\n"
    )
    keys = sorted(d.debt.key for d in patched(make_repo, source) if d.debt)
    assert keys == [
        "test_x.py#patch.multiple lib.mod.REGISTRY",
        "test_x.py#patch.multiple lib.mod.helper",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', lambda s: None)\n",
        "import os\nfrom unittest.mock import patch\n\ndef test_a():\n    with patch.dict(os.environ, {'A': '1'}):\n        pass\n",
        "from unittest import mock\nimport json\n\ndef test_a():\n    with mock.patch.object(json, 'dumps'):\n        pass\n",
        "def test_a(monkeypatch):\n    monkeypatch.setenv('A', '1')\n",
        "class Local:\n    def go(self): ...\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(Local, 'go', lambda self: None)\n",
        "def test_a(monkeypatch, thing):\n    monkeypatch.setattr(thing, 'x', 1)\n    monkeypatch.setattr(thing.inner, 'y', 1)\n",
        "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setitem(mod.REGISTRY, 'a', 1)\n",
    ],
)
def test_third_party_standard_library_local_and_environment_patches_are_not_counted(
    make_repo: RepoFactory, source: str
) -> None:
    assert patched(make_repo, source) == []


def test_a_string_target_stays_a_restricted_form_and_is_not_counted_twice(
    make_repo: RepoFactory,
) -> None:
    source = "from unittest import mock\n\ndef test_a():\n    with mock.patch('lib.mod.helper'):\n        pass\n"
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert len(diagnostics(root, "restricted-python")) == 1
    assert diagnostics(root, "first-party-patch") == []


def test_the_message_says_what_to_do_instead(make_repo: RepoFactory) -> None:
    source = "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None)\n"
    [found] = patched(make_repo, source)
    text = found.render()
    assert "first-party lib.mod.helper" in text
    assert "public facade" in text
    assert "explicit parameters" in text
    assert "pass the fake in" in text


def test_a_patch_can_be_recorded_and_the_directory_becomes_strict_to_be(
    make_repo: RepoFactory,
) -> None:
    source = (
        "from lib import mod\n\ndef test_a(monkeypatch):\n"
        "    monkeypatch.setattr(mod, 'helper', None)\n"
        "    monkeypatch.setattr(mod, 'helper', None)\n"
    )
    root = make_repo({**LIB, "app/__init__.py": "", "app/test_x.py": source})
    assert run(root, "debt", "record", "app") == 0
    git(root, "add", "-A")
    text = (root / "app/meizel.toml").read_text()
    assert text == '[debt.first-party-patch]\n"test_x.py#monkeypatch.setattr lib.mod.helper" = 2\n'
    assert [d for d in analysis_of(root).verdict if d.code.value == "first-party-patch"] == []


def test_a_new_patch_in_a_strict_to_be_directory_fails(make_repo: RepoFactory) -> None:
    source = "from lib import mod\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', None)\n    monkeypatch.setattr(mod, 'other', None)\n"
    baseline = '[debt.first-party-patch]\n"test_x.py#monkeypatch.setattr lib.mod.helper" = 1\n'
    root = make_repo(
        {**LIB, "app/__init__.py": "", "app/test_x.py": source, "app/meizel.toml": baseline}
    )
    [found] = [d for d in analysis_of(root).verdict if d.code.value == "first-party-patch"]
    assert "lib.mod.other" in found.message
    assert "app is strict-to-be" in found.render()
