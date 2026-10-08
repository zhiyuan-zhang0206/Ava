"""Unit coverage for scripts/structure/baseline_shards.py: splitting a merged
baseline into per-directory shards, rendering/merging them back, and reading
them from a working tree or a revision (no lcs.main — see
test_lint_code_structure.py and test_baseline_shard_validity_gate.py for the
gate wiring)."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts.structure import baseline_shards


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed test commands, never external input.
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "kind,key,expected",
    [
        # directories: the key itself is the directory.
        ("directories", "base", "base"),
        ("directories", "cli/commands", "cli/commands"),
        # A plain file path: its parent directory decides.
        ("files", "scripts/lint_x.py", "scripts"),
        ("files", "agent/graph/x.py", "agent/graph"),
        # `path::target` (complexity/nesting/private_imports/owner_bypasses/
        # path_imports): the path half's parent directory decides.
        ("complexity", "agent/graph/x.py::f", "agent/graph"),
        ("private_imports", "agent/graph/x.py::base._priv", "agent/graph"),
        ("owner_bypasses", "gateway/db.py::postgres-dial", "gateway"),
    ],
)
def test_shard_of_by_key_kind(kind: str, key: str, expected: str) -> None:
    assert baseline_shards.shard_of(kind, key) == expected


def test_shard_path_is_the_repo_relative_shard_file() -> None:
    assert (
        baseline_shards.shard_path("files", "scripts/lint_x.py")
        == "scripts/structure/baseline/scripts.json"
    )
    assert baseline_shards.shard_path("patch_targets", "base/db/test_pool.py::base.db._pool") == (
        "scripts/structure/baseline/base/db.json"
    )


def test_split_render_merge_round_trips() -> None:
    """split -> render -> merge reproduces the original baseline exactly, with
    each entry filed under its own directory's shard and empty sections
    omitted from a shard that has no entries in them."""
    baseline = {
        "directories": {"base": 25, "cli/commands": 30},
        "files": {"scripts/lint_x.py": 900, "agent/graph/x.py": 850},
        "complexity": {"agent/graph/x.py::f": 16},
        "nesting": {},
        "private_imports": {"agent/graph/x.py::base._priv": 1},
        "owner_bypasses": {"gateway/db.py::postgres-dial": 1},
        "path_imports": {},
    }

    shards = baseline_shards.split(baseline)

    assert shards == {
        "base": {"directories": {"base": 25}},
        "cli/commands": {"directories": {"cli/commands": 30}},
        "scripts": {"files": {"scripts/lint_x.py": 900}},
        "agent/graph": {
            "files": {"agent/graph/x.py": 850},
            "complexity": {"agent/graph/x.py::f": 16},
            "private_imports": {"agent/graph/x.py::base._priv": 1},
        },
        "gateway": {"owner_bypasses": {"gateway/db.py::postgres-dial": 1}},
    }

    texts = {name: baseline_shards.render(shard) for name, shard in shards.items()}
    restored = baseline_shards.merge(texts, tuple(baseline))

    assert restored == baseline


def test_merge_fills_every_requested_section_even_when_no_shard_has_it() -> None:
    restored = baseline_shards.merge({}, ("directories", "files", "complexity"))

    assert restored == {"directories": {}, "files": {}, "complexity": {}}


def test_render_is_canonical_sorted_json() -> None:
    shard = {"files": {"b/x.py": 900, "a/y.py": 850}}

    text = baseline_shards.render(shard)

    assert text == json.dumps({"files": {"a/y.py": 850, "b/x.py": 900}}, indent=2) + "\n"
    # Idempotent: rendering the parsed-back shard reproduces the same text.
    assert baseline_shards.render(json.loads(text)) == text


def test_merge_preserves_an_entry_in_its_original_shard_after_a_move() -> None:
    """Storage names do not constrain file moves or change frozen permissions."""
    texts = {"base": json.dumps({"files": {"agent/graph/x.py": 900}})}
    assert baseline_shards.merge(texts, ("files",)) == {"files": {"agent/graph/x.py": 900}}


def test_merge_rejects_duplicate_entries_across_shards() -> None:
    entry = {"files": {"agent/graph/x.py": 900}}
    texts = {"base": json.dumps(entry), "agent/graph": json.dumps(entry)}
    with pytest.raises(ValueError) as exc_info:
        baseline_shards.merge(texts, ("files",))

    message = str(exc_info.value)
    assert "base.json" in message
    assert "agent/graph/x.py" in message
    assert "duplicates files entry" in message


def test_merge_rejects_an_unknown_section() -> None:
    texts = {"scripts": json.dumps({"bogus": {}})}

    with pytest.raises(ValueError, match=r"scripts\.json has unknown section 'bogus'"):
        baseline_shards.merge(texts, ("files",))


def test_merge_rejects_a_non_object_shard() -> None:
    with pytest.raises(ValueError, match=r"base\.json must be an object"):
        baseline_shards.merge({"base": "[]"}, ("files",))


def test_merge_rejects_a_non_object_section() -> None:
    with pytest.raises(ValueError, match=r"scripts\.json section 'files' must be an object"):
        baseline_shards.merge({"scripts": json.dumps({"files": []})}, ("files",))


def test_read_worktree_requires_the_shard_directory_to_exist(tmp_path: pathlib.Path) -> None:
    """A missing directory can only be an accidental deletion: the README.md
    that keeps git tracking the directory is committed even with zero shards."""
    with pytest.raises(ValueError, match=r"baseline directory missing.*README"):
        baseline_shards.read_worktree(tmp_path)


def test_read_at_returns_an_empty_dict_for_a_committed_readme_only_directory(
    tmp_path: pathlib.Path,
) -> None:
    """A shard directory with only its README.md (no *.json) committed at a
    revision is a real, comparable empty baseline — an empty dict, not the None
    that means the revision predates the shard scheme entirely."""
    directory = tmp_path / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Empty baseline, fully paid off")

    assert baseline_shards.read_at(tmp_path, "HEAD") == {}


def test_read_at_returns_none_before_the_shard_directory_ever_existed(
    tmp_path: pathlib.Path,
) -> None:
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "Before the shard scheme existed")

    assert baseline_shards.read_at(tmp_path, "HEAD") is None


def test_read_at_returns_every_committed_shard_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """The shards come back from one batched read; each must match what `git show`
    prints for that file, multi-byte text and a trailing-newline-free shard included,
    and the rules file and README stay out."""
    directory = tmp_path / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True)
    contents = {
        "agent.json": '{"files": {"agent/a.py": 801}}\n',
        "base.db.json": '{"files": {"base/db/é.py": 900}}',  # no final newline
        "scripts.json": '{"directories": {"scripts": 30}}\n',
    }
    for name, text in contents.items():
        (directory / name).write_text(text, encoding="utf-8")
    (directory / baseline_shards.RULES_FILE).write_text('{"patch_targets": 2}\n', encoding="utf-8")
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Three shards")

    assert baseline_shards.read_at(tmp_path, "HEAD") == {
        name.removesuffix(".json"): text for name, text in contents.items()
    }


def test_nested_storage_matches_its_flat_history_and_preserves_rules(
    tmp_path: pathlib.Path,
) -> None:
    directory = tmp_path / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True)
    sections = ("patch_targets", "ambient_state")
    contents = {
        "base.db.json": '{"ambient_state": {"base/db/pool.py::pool": 1}}\n',
        "agent.db.json": '{"patch_targets": {"agent/db/test_pool.py::base.db._pool": 2}}',
        "base.rules.json": '{"ambient_state": {"base/rules.py::registry": 1}}\n',
    }
    for name, text in contents.items():
        (directory / name).write_text(text, encoding="utf-8")
    (directory / "rules.json").write_text('{"patch_targets": 2}\n', encoding="utf-8")
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Flat shards")
    flat = baseline_shards.read_at(tmp_path, "HEAD")
    assert flat is not None
    assert flat == {name.removesuffix(".json"): text for name, text in contents.items()}
    for name in contents:
        component, area = name.split(".", 1)
        (directory / component).mkdir(exist_ok=True)
        _git(
            tmp_path,
            "mv",
            f"{baseline_shards.SHARD_DIR}/{name}",
            f"{baseline_shards.SHARD_DIR}/{component}/{area}",
        )
    nested = baseline_shards.read_worktree(tmp_path)
    assert nested == {
        name.replace(".", "/", 1).removesuffix(".json"): text for name, text in contents.items()
    }
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Component folders")
    assert baseline_shards.read_at(tmp_path, "HEAD") == nested
    assert baseline_shards.read_at(tmp_path, "HEAD~1") == flat
    assert baseline_shards.merge(nested, sections) == baseline_shards.merge(flat, sections)
    assert baseline_shards.read_rules(tmp_path, "HEAD~1") == (
        {"patch_targets": 2},
        {"patch_targets": 2},
    )


def test_nested_shards_cannot_hide_duplicate_sites(tmp_path: pathlib.Path) -> None:
    directory = tmp_path / baseline_shards.SHARD_DIR
    text = '{"patch_targets": {"agent/tests/test_x.py::base.db._pool": 1}}\n'
    for component in ("base", "agent"):
        (directory / component).mkdir(parents=True)
        (directory / component / "db.json").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicates patch_targets entry"):
        baseline_shards.merge(baseline_shards.read_worktree(tmp_path), ("patch_targets",))
