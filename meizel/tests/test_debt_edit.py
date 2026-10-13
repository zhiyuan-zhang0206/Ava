"""`debt record` and `debt prune`: the only commands that change a baseline."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.contracts.debt import set_debt
from meizel.graph.model import Code
from meizel.tests.synthetic import RepoFactory, analysis_of, failures, git, run, write

DIRTY = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import _secret, _other\nfrom pkg.b import _secret as again\n",
    "pkg/b.py": "_secret = 1\n_other = 2\n",
    "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n',
    "app/__init__.py": "",
    "app/run.py": "exec('x = 1')\n",
}


def record(root: Path, *paths: str) -> int:
    """`debt record`, then stage what it wrote: `check` reads tracked files only."""
    code = run(root, "debt", "record", *paths)
    git(root, "add", "-A")
    return code


def debt_of(root: Path, directory: str) -> dict[str, dict[str, int]]:
    path = root / directory / "meizel.toml" if directory else root / "meizel.toml"
    return tomllib.loads(path.read_text()).get("debt", {})


def test_record_writes_the_current_violations_into_the_owning_manifest(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(DIRTY)
    assert record(root, ".") == 0
    assert debt_of(root, "pkg") == {
        "private-name": {"a.py -> b.py#_secret": 2, "a.py -> b.py#_other": 1}
    }
    assert debt_of(root, "app") == {"restricted-python": {"run.py#exec of source text": 1}}
    assert failures(root) == set()


def test_check_and_bootstrap_never_record_anything(make_repo: RepoFactory, tmp_path: Path) -> None:
    root = make_repo(DIRTY)
    assert run(root, "check") == 1
    assert run(root, "bootstrap", "--out", str(tmp_path / "out")) == 0
    assert debt_of(root, "pkg") == {}
    assert not list((tmp_path / "out").rglob("meizel.toml")) or all(
        "debt" not in p.read_text() for p in (tmp_path / "out").rglob("meizel.toml")
    )


def test_record_text_is_sorted_one_debt_per_line_and_stable(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, ".") == 0
    text = (root / "pkg/meizel.toml").read_text()
    assert text == (
        '[uses]\n"a.py" = ["b.py"]\n\n'
        "[debt.private-name]\n"
        '"a.py -> b.py#_other" = 1\n'
        '"a.py -> b.py#_secret" = 2\n'
    )
    assert record(root, ".") == 0
    assert (root / "pkg/meizel.toml").read_text() == text


def test_record_covers_only_the_violations_under_the_given_paths(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, "app") == 0
    assert debt_of(root, "app") != {}
    assert debt_of(root, "pkg") == {}
    assert failures(root) == {("private-name", "pkg/a.py")}


def test_record_creates_a_manifest_for_an_owner_that_has_none(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert not (root / "app/meizel.toml").exists()
    assert record(root, "app") == 0
    assert (root / "app/meizel.toml").read_text() == (
        '[debt.restricted-python]\n"run.py#exec of source text" = 1\n'
    )


def test_record_keeps_comments_and_other_sections_and_never_lowers_a_count(
    make_repo: RepoFactory,
) -> None:
    manifest = (
        "# why this directory looks like this\n"
        'exports = ["b.py"]\n\n[uses]\n"a.py" = ["b.py"]  # needed\n\n'
        '[debt.private-name]\n"a.py -> b.py#_secret" = 5\n'
    )
    root = make_repo({**DIRTY, "pkg/meizel.toml": manifest})
    assert record(root, "pkg") == 0
    text = (root / "pkg/meizel.toml").read_text()
    assert text.startswith(
        '# why this directory looks like this\nexports = ["b.py"]\n\n[uses]\n"a.py" = ["b.py"]  # needed\n\n'
    )
    assert debt_of(root, "pkg")["private-name"]["a.py -> b.py#_secret"] == 5


def test_record_puts_a_cross_package_debt_in_the_common_directory(make_repo: RepoFactory) -> None:
    files = {
        "top/__init__.py": "",
        "top/x/__init__.py": "",
        "top/x/m.py": "from top.y.n import _hidden\n",
        "top/y/__init__.py": "",
        "top/y/n.py": "_hidden = 1\n",
        "top/y/meizel.toml": 'exports = ["n.py"]\n',
        "top/meizel.toml": '[uses]\n"x" = ["y"]\n',
    }
    root = make_repo(files)
    assert record(root, "top/x/m.py") == 0
    assert debt_of(root, "top") == {"private-name": {"x/m.py -> y/n.py#_hidden": 1}}
    assert failures(root) == set()


def test_record_chooses_back_edges_that_leave_the_graph_acyclic(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\nfrom pkg import c\n",
        "pkg/b.py": "from pkg import c\n",
        "pkg/c.py": "from pkg import a\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py", "c.py"]\n"b.py" = ["c.py"]\n"c.py" = ["a.py"]\n',
    }
    root = make_repo(files)
    assert failures(root) == {("manifest-cycle", "pkg/meizel.toml")}
    assert record(root, ".") == 0
    recorded = debt_of(root, "pkg")["manifest-cycle"]
    assert recorded == {"c.py -> a.py": 1}
    assert failures(root) == set()


def test_record_keeps_the_back_edges_it_chooses_down_to_the_ones_needed(
    make_repo: RepoFactory,
) -> None:
    names = ["a", "b", "c", "d"]
    files = {"pkg/__init__.py": ""} | {
        f"pkg/{n}.py": "".join(f"from pkg import {m}\n" for m in names if m != n) for n in names
    }
    uses = "".join(
        f'"{n}.py" = [{", ".join(chr(34) + m + ".py" + chr(34) for m in names if m != n)}]\n'
        for n in names
    )
    root = make_repo({**files, "pkg/meizel.toml": "[uses]\n" + uses})
    assert record(root, ".") == 0
    assert failures(root) == set()
    assert len(debt_of(root, "pkg")["manifest-cycle"]) == 6
    assert run(root, "debt", "prune") == 0
    assert len(debt_of(root, "pkg")["manifest-cycle"]) == 6


def test_record_leaves_what_it_cannot_record_failing(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({**DIRTY, "app/other.py": "import lib.missing\n", "lib/__init__.py": ""})
    assert record(root, ".") == 1
    err = capsys.readouterr().err
    assert "cannot be recorded" in err
    assert "unresolved-import" in err
    assert debt_of(root, "pkg") != {}


def test_record_refuses_to_rewrite_a_malformed_baseline(make_repo: RepoFactory) -> None:
    root = make_repo({**DIRTY, "pkg/meizel.toml": '[debt.mystery]\n"x" = 1\n'})
    assert record(root, ".") == 2
    assert (root / "pkg/meizel.toml").read_text() == '[debt.mystery]\n"x" = 1\n'


def test_record_rejects_a_path_outside_the_repository(make_repo: RepoFactory) -> None:
    assert run(make_repo(DIRTY), "debt", "record", "/") == 2


def paid_in_part(root: Path) -> None:
    write(
        root,
        {
            "pkg/a.py": "from pkg.b import _secret\n",
            "app/run.py": "VALUE = 1\n",
        },
    )


def test_prune_lowers_counts_and_drops_what_is_paid(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, ".") == 0
    paid_in_part(root)
    assert "debt-paid" in {c for c, _ in failures(root)}
    assert run(root, "debt", "prune") == 0
    assert debt_of(root, "pkg") == {"private-name": {"a.py -> b.py#_secret": 1}}
    assert not (root / "app/meizel.toml").exists()
    assert failures(root) == set()


def test_prune_never_raises_a_count_and_never_adds_an_entry(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, "app") == 0
    write(root, {"pkg/a.py": "from pkg.b import _secret, _other, _third\n"})
    before = (root / "app/meizel.toml").read_text()
    assert run(root, "debt", "prune") == 0
    assert (root / "app/meizel.toml").read_text() == before
    assert debt_of(root, "pkg") == {}


def test_a_directory_whose_debt_is_all_paid_becomes_strict_again(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, "pkg") == 0
    write(root, {"pkg/a.py": "from pkg import b\n"})
    assert run(root, "debt", "prune") == 0
    assert (root / "pkg/meizel.toml").read_text() == '[uses]\n"a.py" = ["b.py"]\n'
    write(root, {"pkg/a.py": "from pkg.b import _secret\n"})
    [found] = [d for d in analysis_of(root).verdict if d.path == "pkg/a.py"]
    assert "strict-to-be" not in found.render()


def test_prune_pays_cycle_back_edges_one_at_a_time(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg import b\n",
        "pkg/b.py": "from pkg import a\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n',
    }
    root = make_repo(files)
    assert record(root, ".") == 0
    assert len(debt_of(root, "pkg")["manifest-cycle"]) == 1
    write(root, {"pkg/b.py": "VALUE = 1\n", "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n'})
    assert run(root, "debt", "prune") == 0
    assert debt_of(root, "pkg") == {}
    assert failures(root) == set()


def test_prune_refuses_to_rewrite_a_malformed_baseline(make_repo: RepoFactory) -> None:
    root = make_repo({**DIRTY, "pkg/meizel.toml": '[debt.mystery]\n"x" = 1\n'})
    assert run(root, "debt", "prune") == 2


def test_sync_prune_leaves_a_debt_table_alone(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, ".") == 0
    write(
        root,
        {
            "pkg/meizel.toml": (root / "pkg/meizel.toml")
            .read_text()
            .replace(
                '[uses]\n"a.py" = ["b.py"]\n',
                '[uses]\n"a.py" = ["b.py"]\n"b.py" = []\n"__init__.py" = ["a.py"]\n',
            )
        },
    )
    assert run(root, "sync", "--prune") == 0
    assert "__init__.py" not in (root / "pkg/meizel.toml").read_text()
    assert debt_of(root, "pkg") != {}


def test_bootstrap_carries_recorded_debt_into_its_mirror(
    make_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_repo(DIRTY)
    assert record(root, ".") == 0
    out = tmp_path / "out"
    assert run(root, "bootstrap", "--out", str(out)) == 0
    mirrored = tomllib.loads((out / "pkg/meizel.toml").read_text())
    assert mirrored["debt"] == debt_of(root, "pkg")


def test_set_debt_replaces_only_the_debt_block() -> None:
    text = (
        'exports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n\n'
        '[debt.private-name]\n"x -> y#_z" = 1\n\n[debt.restricted-python]\n"f.py#exec" = 2\n'
    )
    assert set_debt(text, {Code.PRIVATE_NAME: {"x -> y#_z": 3}}) == (
        'exports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n\n[debt.private-name]\n"x -> y#_z" = 3\n'
    )
    assert set_debt(text, {}) == 'exports = ["a.py"]\n\n[uses]\n"a.py" = ["b.py"]\n'
    assert set_debt('[debt.private-name]\n"x -> y#_z" = 1\n', {}) == ""
    quoted = {Code.RESTRICTED: {'f.py#mock.patch "a.b"': 1}}
    assert (
        tomllib.loads(set_debt("", quoted))["debt"]["restricted-python"] == quoted[Code.RESTRICTED]
    )


def test_untracked_work_is_not_seen_by_the_baseline_until_staged(make_repo: RepoFactory) -> None:
    root = make_repo(DIRTY)
    assert record(root, "app") == 0
    git(root, "reset", "-q", "app/meizel.toml")
    assert ("restricted-python", "app/run.py") in failures(root)
