from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, run, write

BASE = (
    "[debt.first-party-patch]\n"
    '"test_p.py#monkeypatch.setattr lib.mod.helper" = 5\n'
    '"test_p.py#monkeypatch.setattr lib.mod.other" = 2\n'
    '"test_q.py#monkeypatch.setattr base.config.settings.agent.x" = 4\n'
    '"test_q.py#monkeypatch.setattr base.config.settings.agent.y" = 1\n'
    "\n[debt.stdlib-patch]\n"
    '"test_p.py#monkeypatch.setattr time.sleep" = 3\n'
    "\n[debt.private-name]\n"
    '"x.py -> y.py#_n" = 3\n'
)
AFTER = (
    "[debt.first-party-patch]\n"
    '"test_p.py#monkeypatch.setattr lib.mod.helper" = 1\n'
    '"test_p.py#monkeypatch.setattr lib.mod.other" = 2\n'
    '"test_p.py#monkeypatch.setattr lib.mod3.f" = 2\n'
    '"test_q.py#monkeypatch.setattr base.config.settings.agent.y" = 1\n'
    "\n[debt.stdlib-patch]\n"
    '"test_p.py#monkeypatch.setattr time.sleep" = 3\n'
    "\n[debt.private-name]\n"
    '"x.py -> y.py#_n" = 9\n'
)


def repo(make_repo: RepoFactory) -> Path:
    root = make_repo({"a/__init__.py": "", "a/meizel.toml": BASE, "ava/__init__.py": ""})
    commit(root)
    write(root, {"a/meizel.toml": AFTER})
    return root


def table(
    root: Path, capsys: pytest.CaptureFixture[str], *flags: str, extra: tuple[str, ...] = ()
) -> list[list[str]]:
    run(
        root,
        "debt",
        "diff",
        "--against",
        "HEAD",
        "--on-growth",
        "warn",
        *extra,
        "--by-target",
        *flags,
    )
    out = capsys.readouterr().out
    section = out.split("\n\n")[1] if "target" in out.split("\n\n")[1] else out.split("\n\n")[0]
    return [line.split() for line in section.strip().splitlines()]


def test_the_default_groups_are_the_first_three_names_of_a_target(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = table(repo(make_repo), capsys)
    assert rows[0][:2] == ["target", "code"]
    assert rows[1:] == [
        ["base.config.settings", "first-party-patch", "5", "1", "-4"],
        ["lib.mod.helper", "first-party-patch", "5", "1", "-4"],
        ["lib.mod3.f", "first-party-patch", "0", "2", "+2"],
        ["all", "patch", "debt", "15", "9", "-6"],
    ]


def test_unchanged_groups_and_other_codes_stay_out(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    flat = [cell for row in table(repo(make_repo), capsys) for cell in row]
    assert "lib.mod.other" not in flat
    assert "time.sleep" not in flat
    assert "private-name" not in flat


def test_a_depth_makes_coarser_groups(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = table(repo(make_repo), capsys, "--depth", "2")
    assert rows[1:] == [
        ["base.config", "first-party-patch", "5", "1", "-4"],
        ["lib.mod", "first-party-patch", "7", "3", "-4"],
        ["lib.mod3", "first-party-patch", "0", "2", "+2"],
        ["all", "patch", "debt", "15", "9", "-6"],
    ]


def test_listed_prefixes_are_the_groups_and_always_show(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = table(
        repo(make_repo),
        capsys,
        "--target-prefix",
        "base.config.settings",
        "--target-prefix",
        "lib.mod",
        "--target-prefix",
        "time",
    )
    assert rows[1:] == [
        ["base.config.settings", "first-party-patch", "5", "1", "-4"],
        ["lib.mod", "first-party-patch", "7", "3", "-4"],
        ["time", "stdlib-patch", "3", "3", "+0"],
        ["(other)", "first-party-patch", "0", "2", "+2"],
        ["all", "patch", "debt", "15", "9", "-6"],
    ]


def test_the_longest_listed_prefix_takes_the_target(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = table(
        repo(make_repo),
        capsys,
        "--target-prefix",
        "lib.mod",
        "--target-prefix",
        "lib.mod.helper",
    )
    names = {row[0]: row[4] for row in rows[1:-1]}
    assert names["lib.mod.helper"] == "-4"
    assert names["lib.mod"] == "+0"


def test_debt_that_moves_with_its_file_stays_under_its_target(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({"a/__init__.py": "", "a/meizel.toml": BASE, "b/__init__.py": ""})
    commit(root)
    write(root, {"a/meizel.toml": "", "b/meizel.toml": BASE})
    run(root, "debt", "diff", "--against", "HEAD", "--on-growth", "fail", "--by-target")
    out = capsys.readouterr().out
    assert "+0" in out
    assert "-" not in out.split("all patch debt")[1].split()[2]


def test_a_ref_with_a_retired_code_is_still_readable(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "a/__init__.py": "",
            "a/meizel.toml": '[debt.manifest-cycle]\n"x.py -> y.py" = 2\n' + BASE,
        }
    )
    commit(root)
    write(root, {"a/meizel.toml": AFTER})
    assert table(root, capsys)[-1][-1] == "-6"


def test_the_exit_status_still_follows_on_growth(make_repo: RepoFactory) -> None:
    root = repo(make_repo)
    assert run(root, "debt", "diff", "--against", "HEAD", "--on-growth", "fail", "--by-target") == 1
