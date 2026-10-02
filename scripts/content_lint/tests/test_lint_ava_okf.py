"""`scripts/content_lint/lint_ava_okf.py` — gate-level behavior.

A typo'd explicit target must fail the gate: an explicit path argument that
does not exist must report the missing target on stderr and exit 1, not print
"No .ava.okf.md files found." and exit 0.

E009 (overview position) is judged on the logical path, so a node in a
package's `docs/` layer is checked as if it sat where the layer sits, and a
directory counts as existing when any node's logical path lies under it. E014
(docs layer) requires every node outside `okf/` and `.github/` to sit in a
`docs/` layer and names where it belongs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts.content_lint import lint_ava_okf as gate


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd explicit path must fail, not exit 0 with a confusing message."""
    good = tmp_path / "ok.ava.okf.md"
    good.write_text("# placeholder\n", encoding="utf-8")
    missing = tmp_path / "typo.ava.okf.md"
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py", str(missing)])
    with pytest.raises(SystemExit) as exc:
        gate.main()
    assert exc.value.code == 1
    assert str(missing) in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py", str(good), str(missing)])
    with pytest.raises(SystemExit) as exc2:
        gate.main()
    assert exc2.value.code == 1


_NODE = "---\ntype: doc\ntitle: T\ndescription: D\n---\n\n# T\n"


def _lint_codes(repo_root: Path, rel: str, code: str, *others: str) -> list[str]:
    """The `code` messages the gate reports for `rel` inside the repo `repo_root`,
    after writing `others` (further nodes of the same tree) beside it."""
    for other in others:
        (repo_root / other).parent.mkdir(parents=True, exist_ok=True)
        (repo_root / other).write_text(_NODE, encoding="utf-8")
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE, encoding="utf-8")
    root = repo_root.resolve()
    errors = gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)
    return [e.msg for e in errors if e.code == code]


def _e009(repo_root: Path, rel: str, *others: str) -> list[str]:
    return _lint_codes(repo_root, rel, "E009", *others)


def _e014(repo_root: Path, rel: str, *others: str) -> list[str]:
    return _lint_codes(repo_root, rel, "E014", *others)


def test_e009_reports_a_sibling_overview_beside_its_directory(tmp_path: Path) -> None:
    (tmp_path / "agent" / "foo").mkdir(parents=True)
    (msg,) = _e009(tmp_path, "agent/foo.ava.okf.md")
    assert "'agent/foo/foo.ava.okf.md'" in msg


def test_e009_reports_a_layered_sibling_overview(tmp_path: Path) -> None:
    """`agent/docs/foo.ava.okf.md` sits logically at `agent/foo.ava.okf.md`."""
    (tmp_path / "agent" / "foo").mkdir(parents=True)
    (msg,) = _e009(tmp_path, "agent/docs/foo.ava.okf.md")
    assert "'agent/foo/docs/foo.ava.okf.md'" in msg


def test_e009_reports_a_repo_root_layered_sibling_overview(tmp_path: Path) -> None:
    (tmp_path / "foo").mkdir()
    (msg,) = _e009(tmp_path, "docs/foo.ava.okf.md")
    assert "'foo/docs/foo.ava.okf.md'" in msg


def test_e009_accepts_overviews_inside_their_directory(tmp_path: Path) -> None:
    (tmp_path / "agent" / "foo").mkdir(parents=True)
    assert _e009(tmp_path, "agent/foo/foo.ava.okf.md") == []
    assert _e009(tmp_path, "agent/foo/docs/foo.ava.okf.md") == []
    # A layered document that only shares a name with no directory is fine too.
    assert _e009(tmp_path, "agent/docs/bar.ava.okf.md") == []


_FOLDED = (
    "agent/graph/docs/context-notes/context-notes.ava.okf.md",
    "agent/graph/docs/context-notes/memory-injection.ava.okf.md",
)


def test_e009_reports_a_sibling_beside_a_folded_doc_only_directory(tmp_path: Path) -> None:
    """A doc-only directory keeps its nodes in `agent/graph/docs/context-notes/`, so no
    `agent/graph/context-notes/` exists on disk; the logical path still makes it one."""
    (tmp_path / "agent" / "graph").mkdir(parents=True)
    (msg,) = _e009(tmp_path, "agent/graph/docs/context-notes.ava.okf.md", *_FOLDED)
    assert "'agent/graph/docs/context-notes/context-notes.ava.okf.md'" in msg


def test_e009_accepts_the_overview_inside_a_folded_doc_only_directory(tmp_path: Path) -> None:
    assert _e009(tmp_path, _FOLDED[0], _FOLDED[1]) == []
    assert _e009(tmp_path, _FOLDED[1], _FOLDED[0]) == []


def test_e009_ignores_a_folded_directory_of_another_parent(tmp_path: Path) -> None:
    """Only nodes logically under `<parent>/<stem>/` make the directory exist."""
    assert _e009(tmp_path, "agent/docs/context-notes.ava.okf.md", *_FOLDED) == []


def test_e014_names_the_layer_beside_the_nearest_code(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "loop.py").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "agent/foo.ava.okf.md")
    assert "'agent/docs/foo.ava.okf.md'" in msg
    assert "okf/ and .github/ are exempt" in msg


def test_e014_keeps_the_subdirectory_below_the_layer(tmp_path: Path) -> None:
    """A doc-only subdirectory folds into the nearest code directory's layer."""
    (tmp_path / "agent" / "graph").mkdir(parents=True)
    (tmp_path / "agent" / "graph" / "state.py").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "agent/graph/notes/x.ava.okf.md")
    assert "'agent/graph/docs/notes/x.ava.okf.md'" in msg


def test_e014_treats_skill_and_package_manifests_as_code(tmp_path: Path) -> None:
    (tmp_path / "skills" / "s").mkdir(parents=True)
    (tmp_path / "skills" / "s" / "SKILL.md").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "skills/s/sub/x.ava.okf.md")
    assert "'skills/s/docs/sub/x.ava.okf.md'" in msg
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}", encoding="utf-8")
    (msg,) = _e014(tmp_path, "web/y.ava.okf.md")
    assert "'web/docs/y.ava.okf.md'" in msg


def test_e014_falls_back_to_the_nodes_own_directory(tmp_path: Path) -> None:
    """No code above: the layer goes in the directory the node already sits in."""
    (msg,) = _e014(tmp_path, "notes/x.ava.okf.md")
    assert "'notes/docs/x.ava.okf.md'" in msg
    (msg,) = _e014(tmp_path, "top.ava.okf.md")
    assert "'docs/top.ava.okf.md'" in msg


def test_e014_accepts_layered_and_exempt_nodes(tmp_path: Path) -> None:
    assert _e014(tmp_path, "agent/docs/foo.ava.okf.md") == []
    assert _e014(tmp_path, "agent/graph/docs/notes/x.ava.okf.md") == []
    assert _e014(tmp_path, "docs/top.ava.okf.md") == []
    assert _e014(tmp_path, "okf/index.ava.okf.md") == []
    assert _e014(tmp_path, "okf/plugins/module-loading/two-faces.ava.okf.md") == []
    assert _e014(tmp_path, ".github/.github.ava.okf.md") == []


def test_e014_blocks_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An E-level finding: the effective exit code is 1, and a layered tree is clean."""
    (tmp_path / "agent" / "docs").mkdir(parents=True)
    (tmp_path / "agent" / "docs" / "ok.ava.okf.md").write_text(_NODE, encoding="utf-8")
    (tmp_path / "agent" / "stray.ava.okf.md").write_text(_NODE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py"])
    with pytest.raises(SystemExit) as exc:
        gate.main()
    assert exc.value.code == 1
    assert "E014" in capsys.readouterr().out
    (tmp_path / "agent" / "stray.ava.okf.md").unlink()
    gate.main()  # clean tree: returns without exiting non-zero


def _oversized(repo_root: Path, rel: str) -> str:
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE + "x" * (gate.MAX_CHARS + 1) + "\n", encoding="utf-8")
    root = repo_root.resolve()
    errors = gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)
    (msg,) = [e.msg for e in errors if e.code == "E007" and "too large" in e.msg]
    return msg


def test_split_hint_names_the_child_directory_inside_the_layer(tmp_path: Path) -> None:
    assert "'agent/docs/big/'" in _oversized(tmp_path, "agent/docs/big.ava.okf.md")
    # An overview's children sit beside it, in the same layer.
    assert "'agent/graph/docs/'" in _oversized(tmp_path, "agent/graph/docs/graph.ava.okf.md")
