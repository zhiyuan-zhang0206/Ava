"""Object-form patching is a use of the patched name; the string form is restricted."""

from __future__ import annotations

from scripts.meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics

LIB = {"lib/__init__.py": "", "lib/m.py": "def f(): ...\n_hidden = 1\n\nclass K:\n    attr = 1\n"}


def patched(make_repo: RepoFactory, source: str) -> set[tuple[str, tuple[str, ...]]]:
    root = make_repo({**LIB, "t/test_a.py": source})
    return {(f.dst, f.names) for f in analysis_of(root).facts if f.kind.value == "patch"}


def test_monkeypatch_setattr_on_a_module_uses_the_named_attribute(make_repo: RepoFactory) -> None:
    source = "from lib import m\n\ndef test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.setattr(m, 'f', lambda: 1)\n"
    assert patched(make_repo, source) == {("lib/m.py", ("f",))}


def test_delattr_and_attribute_paths_to_a_module_count(make_repo: RepoFactory) -> None:
    source = "import lib.m\n\ndef test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.delattr(lib.m, 'f')\n"
    assert patched(make_repo, source) == {("lib/m.py", ("f",))}


def test_mock_patch_object_and_multiple(make_repo: RepoFactory) -> None:
    source = (
        "from unittest import mock\nfrom unittest.mock import patch\nfrom lib import m\n\n"
        "def test_a():\n"
        "    with mock.patch.object(m, 'f'):\n        pass\n"
        "    with patch.multiple(m, f=1, _hidden=2, autospec=True):\n        pass\n"
    )
    assert patched(make_repo, source) == {("lib/m.py", ("f",)), ("lib/m.py", ("f", "_hidden"))}


def test_patching_a_class_attribute_is_not_a_module_patch(make_repo: RepoFactory) -> None:
    source = "from lib import m\n\ndef test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.setattr(m.K, 'attr', 2)\n"
    assert patched(make_repo, source) == set()


def test_patching_a_private_module_name_is_a_private_use(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **LIB,
            "t/test_a.py": "from lib import m\n\ndef test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.setattr(m, '_hidden', 2)\n",
        }
    )
    [found] = diagnostics(root, "private-name")
    assert (found.path, found.line) == ("t/test_a.py", 4)


def test_other_receivers_named_setattr_are_not_patches(make_repo: RepoFactory) -> None:
    source = "from lib import m\n\ndef test_a(box):\n    box.setattr(m, 'f', 1)\n    setattr(m, 'f', 1)\n"
    assert patched(make_repo, source) == set()
