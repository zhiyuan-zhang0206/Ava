"""`debt sites`: where the source holds each patch of a code, optionally under target prefixes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, run

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "def helper() -> int:\n    return 1\n\n\nclass Client:\n    def send(self) -> None: ...\n",
    "lib/mod2.py": "def other() -> int:\n    return 2\n",
    "lib/meizel.toml": 'exports = ["mod.py", "mod2.py"]\n',
    "meizel.toml": '[uses]\n"app" = ["lib"]\n"more" = ["lib"]\n',
    "more/__init__.py": "",
}
FIRST = (
    "import time\n"
    "from lib import mod, mod2\n"
    "\n"
    "\n"
    "def test_a(monkeypatch):\n"
    "    monkeypatch.setattr(mod, 'helper', lambda: 2)\n"
    "    monkeypatch.setattr(mod.Client, 'send', lambda self: None)\n"
    "    monkeypatch.setattr(mod2, 'other', lambda: 3)\n"
    "    monkeypatch.setattr(time, 'sleep', lambda s: None)\n"
    "\n"
    "\n"
    "def test_b(monkeypatch):\n"
    "    monkeypatch.setattr(mod, 'helper', lambda: 4)\n"
)
SECOND = (
    "from lib import mod\n\n\n"
    "def test_c(monkeypatch):\n"
    "    monkeypatch.setattr(mod, 'helper', lambda: 5)\n"
)


def repo(make_repo: RepoFactory) -> Path:
    return make_repo(
        {**LIB, "app/__init__.py": "", "app/test_x.py": FIRST, "more/test_y.py": SECOND}
    )


def listed(root: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> list[str]:
    assert run(root, "debt", "sites", *flags) == 0
    return capsys.readouterr().out.splitlines()


def test_every_site_of_a_code_is_listed_with_file_line_form_and_target(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(repo(make_repo), capsys, "--code", "first-party-patch")
    assert out == [
        "app/test_x.py:7: monkeypatch.setattr lib.mod.Client.send",
        "app/test_x.py:6: monkeypatch.setattr lib.mod.helper",
        "app/test_x.py:13: monkeypatch.setattr lib.mod.helper",
        "more/test_y.py:5: monkeypatch.setattr lib.mod.helper",
        "app/test_x.py:8: monkeypatch.setattr lib.mod2.other",
        "meizel: 5 sites in 2 files",
    ]


def test_a_target_prefix_keeps_what_is_inside_it_and_not_a_neighbour(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(
        repo(make_repo), capsys, "--code", "first-party-patch", "--target-prefix", "lib.mod"
    )
    assert {line.rpartition(" ")[2] for line in out[:-1]} == {
        "lib.mod.Client.send",
        "lib.mod.helper",
    }
    assert out[-1] == "meizel: 4 sites in 2 files"


def test_a_prefix_may_name_the_whole_target(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(
        repo(make_repo), capsys, "--code", "first-party-patch", "--target-prefix", "lib.mod.helper"
    )
    assert len(out) == 4 and out[-1] == "meizel: 3 sites in 2 files"


def test_several_prefixes_are_a_union(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(
        repo(make_repo),
        capsys,
        "--code",
        "first-party-patch",
        "--target-prefix",
        "lib.mod2",
        "--target-prefix",
        "lib.mod.Client",
    )
    assert out[-1] == "meizel: 2 sites in 1 file"


def test_a_code_lists_only_its_own_patches(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(repo(make_repo), capsys, "--code", "stdlib-patch")
    assert out == ["app/test_x.py:9: monkeypatch.setattr time.sleep", "meizel: 1 site in 1 file"]


def test_paths_restrict_the_scan(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    out = listed(repo(make_repo), capsys, "--code", "first-party-patch", "more")
    assert out == [
        "more/test_y.py:5: monkeypatch.setattr lib.mod.helper",
        "meizel: 1 site in 1 file",
    ]


def test_json_is_a_list_of_sites(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo)
    assert run(root, "debt", "sites", "--code", "first-party-patch", "--json", "more") == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "path": "more/test_y.py",
            "line": 5,
            "code": "first-party-patch",
            "form": "monkeypatch.setattr",
            "target": "lib.mod.helper",
        }
    ]


def test_a_code_with_nothing_to_list_is_an_error(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo)
    assert run(root, "debt", "sites", "--code", "private-name") == 2
    err = capsys.readouterr().err
    assert "private-name" in err and "first-party-patch" in err
    with pytest.raises(SystemExit):
        run(root, "debt", "sites")


def test_the_listing_does_not_depend_on_the_baseline(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(make_repo)
    before = listed(root, capsys, "--code", "first-party-patch")
    assert run(root, "debt", "record", "app", "more") == 0
    capsys.readouterr()
    assert listed(root, capsys, "--code", "first-party-patch") == before
