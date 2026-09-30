"""Structure Rule 8: patch points, their classes, and the lint that freezes class D."""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from scripts.lint import patch_targets as lint
from scripts.structure import baseline_shards, locality, patch_points, patch_targets
from scripts.structure.placement import ModuleIndex
from tests.scripts.structure.patch_repo import make_repo, write

# A test whose subject is `cli.commands.run`: its home is `cli/commands`.
_SUBJECT = "from cli.commands import run\n\nrun.main()\n"


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    locality.reset_caches()
    return make_repo(tmp_path)


def _analyze(root: pathlib.Path, rel: str, text: str) -> patch_targets.FileResult:
    write(root, rel, text)
    return patch_targets.analyze(rel, text, patch_targets.Classifier(ModuleIndex(root)))


def _cats(result: patch_targets.FileResult) -> list[str]:
    return [site.cat for site in result.sites]


# --------------------------------------------------------------------------- forms

_PRIVATE = "base.net.retry._sleep"
_FORMS = {
    "setattr string": ("import pytest\n", "monkeypatch.setattr('base.net.retry._sleep', None)"),
    "delattr string": ("import pytest\n", "monkeypatch.delattr('base.net.retry._sleep')"),
    "setattr object": (
        "from base.net import retry\n",
        "monkeypatch.setattr(retry, '_sleep', None)",
    ),
    "setattr module path": (
        "import base.net.retry\n",
        "monkeypatch.setattr(base.net.retry, '_sleep', None)",
    ),
    "patch": ("from unittest.mock import patch\n", "patch('base.net.retry._sleep')"),
    "patch keyword target": (
        "from unittest.mock import patch\n",
        "patch(target='base.net.retry._sleep')",
    ),
    "patch.object": (
        "from unittest.mock import patch\nfrom base.net import retry\n",
        "patch.object(retry, '_sleep')",
    ),
    "mock.patch.object": (
        "from unittest import mock\nfrom base.net import retry\n",
        "mock.patch.object(retry, '_sleep')",
    ),
    "mocker.patch": ("import pytest\n", "mocker.patch('base.net.retry._sleep')"),
    "mocker.patch.object": (
        "from base.net import retry\n",
        "mocker.patch.object(retry, '_sleep')",
    ),
    "patch.multiple": (
        "from unittest.mock import patch\n",
        "patch.multiple('base.net.retry', _sleep=None)",
    ),
    "patch.dict private table": (
        "from unittest.mock import patch\n",
        "patch.dict('base.net.retry._TABLE', {})",
    ),
}


def _body(kind: str, statement: str) -> str:
    """The same call as a plain statement, a `with` block and a decorator."""
    if kind == "statement":
        return f"def test_x(monkeypatch, mocker):\n    {statement}\n"
    if kind == "with":
        return f"def test_x(monkeypatch, mocker):\n    with {statement}:\n        pass\n"
    return f"@{statement}\ndef test_x(monkeypatch, mocker, *mocks):\n    pass\n"


_CASES = [
    (form, kind)
    for form, (_imports, statement) in _FORMS.items()
    # only the mock library's `patch` is a decorator and a context manager
    for kind in (
        ("statement", "with", "decorator")
        if statement.startswith(("patch", "mock.patch"))
        else ("statement",)
    )
]


@pytest.mark.parametrize(("form", "kind"), _CASES)
def test_every_patch_form_reaching_a_private_name_is_class_d(
    root: pathlib.Path, form: str, kind: str
) -> None:
    imports, statement = _FORMS[form]
    result = _analyze(root, "tests/test_x.py", imports + _SUBJECT + _body(kind, statement))
    dropped = [site for site in result.sites if site.cat == "D"]
    assert len(dropped) == 1, [(s.cat, s.target) for s in result.sites]
    assert dropped[0].key.startswith("base.net.retry._")
    assert dropped[0].owner == "base/net"
    assert dropped[0].relation == "other-unit"
    assert result.home == "cli/commands"


def test_a_public_name_or_an_environment_seam_passes(root: pathlib.Path) -> None:
    body = (
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr('base.net.retry.backoff', None)\n"
        "    monkeypatch.setattr('base.net.retry.REGISTRY', {})\n"
    )
    result = _analyze(root, "tests/test_x.py", _SUBJECT + body)
    assert _cats(result) == ["C", "C"]
    assert patch_targets.violations("tests/test_x.py", result) == {}


def test_red_then_green_the_same_test_pointed_at_a_public_name(root: pathlib.Path) -> None:
    red = "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
    green = "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry.backoff', None)\n"
    write(root, "tests/test_x.py", _SUBJECT + red)
    assert lint.main([], repo_root=root) == 1
    write(root, "tests/test_x.py", _SUBJECT + green)
    assert lint.main([], repo_root=root) == 0


# --------------------------------------------------------------------------- classes


def test_the_classes(root: pathlib.Path) -> None:
    body = (
        "import time\nfrom unittest.mock import patch\n"
        + _SUBJECT
        + "def test_x(monkeypatch, param):\n"
        "    monkeypatch.setattr('time.sleep', None)\n"  # A: stdlib
        "    monkeypatch.setenv('HOME', '/x')\n"  # A: non-AVA env
        "    monkeypatch.setattr(time, 'monotonic', None)\n"  # A: object resolved to stdlib
        "    monkeypatch.setenv('AVA_NOT_A_SETTING', '/x')\n"  # E: AVA_* setting
        "    monkeypatch.setattr('base.config.settings', None)\n"  # E: ambient module
        "    monkeypatch.setattr('base.db.connect', None)\n"  # E: ambient door
        "    monkeypatch.setattr(param, 'thing', None)\n"  # U: unresolved object
        "    monkeypatch.setattr('base.net.retry.time.sleep', None)\n"  # A: module's own import
        "    monkeypatch.setattr('base.db.pool._pool', None)\n"  # D: not the E door, private
        "    monkeypatch.setattr('base.net.retry.REGISTRY.get', None)\n"  # C, deep
        "    patch.dict('os.environ', {'AVA_X': '1'})\n"  # E: AVA_* through patch.dict
    )
    result = _analyze(root, "tests/test_x.py", body)
    assert _cats(result) == ["A", "A", "A", "E", "E", "E", "U", "A", "D", "C", "E"]
    deep = [site for site in result.sites if site.cat == "C"]
    assert [site.deep for site in deep] == [True]


def test_a_test_inside_the_owning_package_may_reach_its_private_names(
    root: pathlib.Path,
) -> None:
    text = (
        "from base.net import retry\n\ndef test_x(monkeypatch):\n"
        "    retry.backoff()\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
    )
    result = _analyze(root, "base/net/tests/test_x.py", text)
    assert (result.home, _cats(result)) == ("base/net", ["B"])


def test_a_test_below_the_owner_may_reach_it_but_a_test_above_it_may_not(
    root: pathlib.Path,
) -> None:
    make_repo(
        root,
        {"base/net/wire/__init__.py": "", "base/net/wire/frame.py": "def _pack():\n    pass\n"},
    )
    below = (
        "from base.net.wire import frame\n\ndef test_x(monkeypatch):\n"
        "    frame._pack()\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
    )
    assert _cats(_analyze(root, "tests/test_below.py", below)) == ["B"]
    above = (
        "from base.net import retry\nfrom base.db import pool\n\ndef test_x(monkeypatch):\n"
        "    retry.backoff()\n    pool.acquire()\n"
        "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
    )
    result = _analyze(root, "tests/test_above.py", above)
    (site,) = [s for s in result.sites if s.cat == "D"]
    assert (result.home, site.relation) == ("base", "ancestor")


def test_a_private_module_segment_is_private_and_owned_by_the_package_above_it(
    root: pathlib.Path,
) -> None:
    text = (
        _SUBJECT
        + "def test_x(monkeypatch):\n    monkeypatch.setattr('ava.agents._client.send', None)\n"
    )
    result = _analyze(root, "tests/test_x.py", text)
    (site,) = result.sites
    assert (site.cat, site.key, site.owner) == ("D", "ava.agents._client", "ava/agents")


def test_a_private_attribute_of_a_class_belongs_to_the_package_of_its_module(
    root: pathlib.Path,
) -> None:
    make_repo(root, {"base/net/wire.py": "class Codec:\n    def _pack(self):\n        pass\n"})
    patch = "    monkeypatch.setattr('base.net.wire.Codec._pack', None)\n"
    inside = "from base.net import retry\n\ndef test_x(monkeypatch):\n    retry.backoff()\n" + patch
    result = _analyze(root, "tests/test_inside.py", inside)
    assert (result.home, _cats(result)) == ("base/net", ["B"])
    outside = _SUBJECT + "def test_x(monkeypatch):\n" + patch
    (site,) = _analyze(root, "tests/test_outside.py", outside).sites
    assert (site.cat, site.key, site.owner) == ("D", "base.net.wire.Codec._pack", "base/net")


def test_relations_of_the_home_to_the_owning_package(root: pathlib.Path) -> None:
    target = "monkeypatch.setattr('base.net.retry._sleep', None)"
    sibling = (
        f"from base.db import pool\n\ndef test_x(monkeypatch):\n    pool.acquire()\n    {target}\n"
    )
    (site,) = [s for s in _analyze(root, "tests/test_sibling.py", sibling).sites if s.cat == "D"]
    assert site.relation == "sibling"
    (top,) = _analyze(
        root, "tests/e2e/test_top.py", _SUBJECT + f"def test_x(monkeypatch):\n    {target}\n"
    ).sites
    assert (top.cat, top.relation) == ("D", "top-level")


def test_a_target_that_is_not_repository_code_is_never_a_violation(root: pathlib.Path) -> None:
    text = (
        "import httpx\nfrom unittest.mock import patch\n\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setattr(httpx, 'get', None)\n"
        "    patch('subprocess.run')\n"
        "    patch('some_package._private.thing')\n"
    )
    assert set(_cats(_analyze(root, "tests/test_x.py", text))) == {"A"}


def test_moving_a_file_into_its_packages_tests_directory_changes_no_verdict(
    root: pathlib.Path,
) -> None:
    text = (
        "from base.net import retry\nfrom base.db import pool\n\ndef test_x(monkeypatch):\n"
        "    retry.backoff()\n    pool.acquire()\n"
        "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
        "    monkeypatch.setattr('base.db.pool._pool', None)\n"
    )
    before = _analyze(root, "tests/base/test_x.py", text)
    after = _analyze(root, "base/tests/test_x.py", text)
    assert before == after
    assert patch_targets.violations("tests/base/test_x.py", before).keys() == {
        "tests/base/test_x.py::base.net.retry._sleep",
        "tests/base/test_x.py::base.db.pool._pool",
    }
    assert patch_targets.violations("base/tests/test_x.py", after).keys() == {
        "base/tests/test_x.py::base.net.retry._sleep",
        "base/tests/test_x.py::base.db.pool._pool",
    }


def test_the_ambient_list_is_an_explicit_constant_with_a_lookup_by_longest_prefix() -> None:
    assert patch_targets.e_lookup("base.config.settings.lm") == ("base.config", "config")
    assert patch_targets.e_lookup("base.paths") == ("base.paths", "paths")
    assert patch_targets.e_lookup("base.paths.sub") is None  # only the door, not the subtree
    assert patch_targets.e_lookup("base.db.pool") is None
    assert patch_targets.SECTION in locality.EXTERNAL_SECTIONS


# --------------------------------------------------------------------------- points


def _points(text: str) -> list[patch_points.Point]:
    return patch_points.extract_points(list(ast.walk(ast.parse(text))))


def test_points_cover_environment_and_ambient_forms() -> None:
    points = _points(
        "import os\nfrom unittest.mock import patch\n\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setenv('AVA_NOT_A_SETTING', 'x')\n"
        "    monkeypatch.delenv('HOME')\n"
        "    monkeypatch.chdir('.')\n"
        "    monkeypatch.setitem(os.environ, 'K', 'v')\n"
        "    patch.dict(os.environ, {'A': '1', 'B': '2'})\n"
    )
    assert [(p.form, p.env) for p in points] == [
        ("env", "AVA_NOT_A_SETTING"),
        ("env", "HOME"),
        ("ambient", None),
        ("env", "K"),
        ("env", "A"),
        ("env", "B"),
    ]


def test_an_object_alias_and_a_path_loaded_module_resolve() -> None:
    points = _points(
        "import importlib.util\nfrom base.net import retry\n\nalias = retry\n"
        "spec = importlib.util.spec_from_file_location('m', 'scripts/lint/x.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(alias, '_sleep', None)\n"
        "    monkeypatch.setattr(mod, '_run', None)\n"
        "    monkeypatch.setattr(mod.subprocess, 'run', None)\n"
    )
    assert [p.dotted for p in points] == [
        "base.net.retry._sleep",
        "scripts.lint.x._run",
        "subprocess.run",
    ]


def test_a_call_result_or_a_parameter_is_unresolved() -> None:
    points = _points(
        "def test_x(monkeypatch, thing):\n"
        "    monkeypatch.setattr(thing, 'a', 1)\n"
        "    monkeypatch.setattr(make(), 'b', 1)\n"
    )
    assert [p.unresolved for p in points] == [True, True]


# --------------------------------------------------------------------------- the lint


def _freeze(root: pathlib.Path, counts: dict[str, int]) -> None:
    shards = baseline_shards.split({patch_targets.SECTION: counts})
    for name, shard in shards.items():
        write(root, f"{baseline_shards.SHARD_DIR}/{name}.json", baseline_shards.render(shard))


_VIOLATION = (
    _SUBJECT + "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
)


def test_a_new_violation_fails_with_a_message_that_says_what_to_do(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(root, "tests/test_x.py", _VIOLATION)
    assert lint.main([], repo_root=root) == 1
    captured = capsys.readouterr()
    assert "tests/test_x.py:5:" in captured.out
    assert "private name `base.net.retry._sleep` of package `base.net`" in captured.out
    assert "public entry point" in captured.out
    assert "Most patched private targets so far: base.net.retry (1)" in captured.err


def test_the_ancestor_relation_gets_its_own_advice(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        root,
        "tests/test_x.py",
        "from base.net import retry\nfrom base.db import pool\n\ndef test_x(monkeypatch):\n"
        "    retry.backoff()\n    pool.acquire()\n"
        "    monkeypatch.setattr('base.net.retry._sleep', None)\n",
    )
    assert lint.main([], repo_root=root) == 1
    out = capsys.readouterr().out
    assert "lives in `base` but patches the private name `base.net.retry._sleep`" in out
    assert "move the test down into `base.net`" in out


def test_a_frozen_site_passes_growth_and_shrinkage_both_fail(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(root, "tests/test_x.py", _VIOLATION)
    _freeze(root, {"tests/test_x.py::base.net.retry._sleep": 1})
    assert lint.main([], repo_root=root) == 0

    two = _VIOLATION + "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
    write(root, "tests/test_x.py", two)
    assert lint.main([], repo_root=root) == 1
    assert "grew above its frozen count 1" in capsys.readouterr().out

    write(root, "tests/test_x.py", _SUBJECT)  # the reach-in was fixed
    assert lint.main([], repo_root=root) == 1
    assert (
        "stale patch_targets entry tests/test_x.py::base.net.retry._sleep"
        in capsys.readouterr().out
    )


def test_a_deleted_file_leaves_a_stale_entry_even_when_only_other_files_are_checked(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(root, "tests/test_other.py", _SUBJECT)
    _freeze(root, {"tests/test_gone.py::base.net.retry._sleep": 1})
    assert lint.main([str(root / "tests/test_other.py")], repo_root=root) == 1
    assert "stale patch_targets entry tests/test_gone.py" in capsys.readouterr().out


def test_explicit_targets_check_only_the_named_test_files(root: pathlib.Path) -> None:
    write(root, "tests/test_clean.py", _SUBJECT)
    write(root, "tests/test_bad.py", _VIOLATION)
    assert lint.main([str(root / "tests/test_clean.py")], repo_root=root) == 0
    assert lint.main([str(root / "tests/test_bad.py")], repo_root=root) == 1


def test_a_test_file_outside_the_scanned_scope_is_ignored(root: pathlib.Path) -> None:
    outside = write(root, "tools/tests/test_bad.py", _VIOLATION)
    assert lint.main([str(outside)], repo_root=root) == 0
    assert lint.all_test_files(root) == []


def test_a_changed_rule_file_or_baseline_shard_triggers_a_full_scan(root: pathlib.Path) -> None:
    write(root, "tests/test_bad.py", _VIOLATION)
    rule = write(root, "scripts/lint/patch_targets.py", "")
    assert lint.main([str(rule)], repo_root=root) == 1


def test_a_missing_target_is_an_error(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = str(root / "tests/nope.py")
    assert lint.main([missing], repo_root=root) == 1
    assert f"target path(s) not found: {missing}" in capsys.readouterr().err


def test_package_tests_directories_are_scanned_too(root: pathlib.Path) -> None:
    write(root, "cli/commands/tests/test_x.py", _VIOLATION)
    assert lint.main([], repo_root=root) == 1
    assert [p.relative_to(root).as_posix() for p in lint.all_test_files(root)] == [
        "cli/commands/tests/test_x.py"
    ]


def test_the_report_counts_every_class_and_never_fails(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(root, "tests/test_x.py", _VIOLATION)
    assert lint.main(["--report"], repo_root=root) == 0
    out = capsys.readouterr().out
    assert "| D | violation: a private name of a package the test does not belong to | 1 |" in out
    assert "| other-unit |" in out
    assert "`base.net.retry`" in out
    assert "## Files placed by the patch-evidence fallback" in out


def test_an_unparseable_test_file_is_reported(
    root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(root, "tests/test_broken.py", "def broken(:\n")
    assert lint.main([], repo_root=root) == 1
    assert "tests/test_broken.py:1: cannot parse" in capsys.readouterr().out


def test_the_baseline_section_is_read_from_the_shards_and_validated(root: pathlib.Path) -> None:
    _freeze(root, {"tests/test_x.py::base.net.retry._sleep": 2, "base/tests/test_y.py::a._b": 1})
    assert patch_targets.read_baseline(root) == {
        "tests/test_x.py::base.net.retry._sleep": 2,
        "base/tests/test_y.py::a._b": 1,
    }
    shard = root / baseline_shards.SHARD_DIR / "tests.json"
    shard.write_text(json.dumps({"patch_targets": {"tests/test_x.py::a._b": 0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid patch_targets entry"):
        patch_targets.read_baseline(root)
