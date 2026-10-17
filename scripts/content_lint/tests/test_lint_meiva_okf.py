from __future__ import annotations

from pathlib import Path

import pytest

from scripts.content_lint import lint_meiva_okf as gate


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.meiva.okf.md"
    good.write_text("# placeholder\n", encoding="utf-8")
    missing = tmp_path / "typo.meiva.okf.md"
    with pytest.raises(SystemExit) as exc:
        gate.main([str(missing)])
    assert exc.value.code == 1
    assert str(missing) in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc2:
        gate.main([str(good), str(missing)])
    assert exc2.value.code == 1


_NODE = "---\ntype: doc\ntitle: T\ndescription: D\n---\n\n# T\n"


def _lint_codes(repo_root: Path, rel: str, code: str, *others: str) -> list[str]:
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
    (msg,) = _e009(tmp_path, "agent/foo.meiva.okf.md")
    assert "'agent/foo/foo.meiva.okf.md'" in msg


def test_e009_reports_a_layered_sibling_overview(tmp_path: Path) -> None:
    (tmp_path / "agent" / "foo").mkdir(parents=True)
    (msg,) = _e009(tmp_path, "agent/docs/foo.meiva.okf.md")
    assert "'agent/foo/docs/foo.meiva.okf.md'" in msg


def test_e009_reports_a_repo_root_layered_sibling_overview(tmp_path: Path) -> None:
    (tmp_path / "foo").mkdir()
    (msg,) = _e009(tmp_path, "docs/foo.meiva.okf.md")
    assert "'foo/docs/foo.meiva.okf.md'" in msg


def test_e009_accepts_overviews_inside_their_directory(tmp_path: Path) -> None:
    (tmp_path / "agent" / "foo").mkdir(parents=True)
    assert _e009(tmp_path, "agent/foo/foo.meiva.okf.md") == []
    assert _e009(tmp_path, "agent/foo/docs/foo.meiva.okf.md") == []
    assert _e009(tmp_path, "agent/docs/bar.meiva.okf.md") == []


_FOLDED = (
    "agent/graph/docs/context-notes/context-notes.meiva.okf.md",
    "agent/graph/docs/context-notes/memory-injection.meiva.okf.md",
)


def test_e009_reports_a_sibling_beside_a_folded_doc_only_directory(tmp_path: Path) -> None:
    (tmp_path / "agent" / "graph").mkdir(parents=True)
    (msg,) = _e009(tmp_path, "agent/graph/docs/context-notes.meiva.okf.md", *_FOLDED)
    assert "'agent/graph/docs/context-notes/context-notes.meiva.okf.md'" in msg


def test_e009_accepts_the_overview_inside_a_folded_doc_only_directory(tmp_path: Path) -> None:
    assert _e009(tmp_path, _FOLDED[0], _FOLDED[1]) == []
    assert _e009(tmp_path, _FOLDED[1], _FOLDED[0]) == []


def test_e009_ignores_a_folded_directory_of_another_parent(tmp_path: Path) -> None:
    assert _e009(tmp_path, "agent/docs/context-notes.meiva.okf.md", *_FOLDED) == []


def test_e014_names_the_layer_beside_the_nearest_code(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "loop.py").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "agent/foo.meiva.okf.md")
    assert "'agent/docs/foo.meiva.okf.md'" in msg
    assert ".github/ is exempt" in msg


def test_e014_keeps_the_subdirectory_below_the_layer(tmp_path: Path) -> None:
    (tmp_path / "agent" / "graph").mkdir(parents=True)
    (tmp_path / "agent" / "graph" / "state.py").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "agent/graph/notes/x.meiva.okf.md")
    assert "'agent/graph/docs/notes/x.meiva.okf.md'" in msg


def test_e014_treats_skill_and_package_manifests_as_code(tmp_path: Path) -> None:
    (tmp_path / "skills" / "s").mkdir(parents=True)
    (tmp_path / "skills" / "s" / "SKILL.md").write_text("", encoding="utf-8")
    (msg,) = _e014(tmp_path, "skills/s/sub/x.meiva.okf.md")
    assert "'skills/s/docs/sub/x.meiva.okf.md'" in msg
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}", encoding="utf-8")
    (msg,) = _e014(tmp_path, "web/y.meiva.okf.md")
    assert "'web/docs/y.meiva.okf.md'" in msg


def test_e014_falls_back_to_the_nodes_own_directory(tmp_path: Path) -> None:
    (msg,) = _e014(tmp_path, "notes/x.meiva.okf.md")
    assert "'notes/docs/x.meiva.okf.md'" in msg
    (msg,) = _e014(tmp_path, "top.meiva.okf.md")
    assert "'docs/top.meiva.okf.md'" in msg


def test_e014_accepts_layered_and_exempt_nodes(tmp_path: Path) -> None:
    assert _e014(tmp_path, "agent/docs/foo.meiva.okf.md") == []
    assert _e014(tmp_path, "agent/graph/docs/notes/x.meiva.okf.md") == []
    assert _e014(tmp_path, "docs/top.meiva.okf.md") == []
    assert _e014(tmp_path, "docs/index.meiva.okf.md") == []
    assert _e014(tmp_path, "docs/plugins/module-loading/two-faces.meiva.okf.md") == []
    assert _e014(tmp_path, ".github/.github.meiva.okf.md") == []


@pytest.mark.parametrize(
    "path", ["okf/index.meiva.okf.md", "okf/plugins/module-loading/two-faces.meiva.okf.md"]
)
def test_e014_has_no_legacy_index_directory_exemption(tmp_path: Path, path: str) -> None:
    assert _e014(tmp_path, path)


def test_e014_blocks_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent" / "docs").mkdir(parents=True)
    (tmp_path / "agent" / "docs" / "ok.meiva.okf.md").write_text(_NODE, encoding="utf-8")
    (tmp_path / "agent" / "stray.meiva.okf.md").write_text(_NODE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        gate.main([])
    assert exc.value.code == 1
    assert "E014" in capsys.readouterr().out
    (tmp_path / "agent" / "stray.meiva.okf.md").unlink()
    gate.main([])


def _oversized(repo_root: Path, rel: str) -> str:
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE + "x" * (gate.MAX_CHARS + 1) + "\n", encoding="utf-8")
    root = repo_root.resolve()
    errors = gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)
    (msg,) = [e.msg for e in errors if e.code == "E007" and "too large" in e.msg]
    return msg


def test_split_hint_names_the_child_directory_inside_the_layer(tmp_path: Path) -> None:
    assert "'agent/docs/big/'" in _oversized(tmp_path, "agent/docs/big.meiva.okf.md")
    assert "'agent/graph/docs/'" in _oversized(tmp_path, "agent/graph/docs/graph.meiva.okf.md")


def test_e001_rejects_a_non_node_extension(tmp_path: Path) -> None:
    path = tmp_path / "probe.md"
    path.write_text(_NODE, encoding="utf-8")
    root = tmp_path.resolve()
    codes = [e.code for e in gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)]
    assert codes == ["E001"]


def _schema_codes(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "agent" / "docs" / "probe.meiva.okf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    root = tmp_path.resolve()
    return [e.code for e in gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)]


def test_a_missing_frontmatter_fails_before_any_field_check(tmp_path: Path) -> None:
    assert _schema_codes(tmp_path, "# no frontmatter\n") == ["E002"]


def test_type_must_be_doc_or_memory(tmp_path: Path) -> None:
    assert _schema_codes(tmp_path, _NODE.replace("type: doc", "type: page")) == ["E002"]


def test_title_and_description_are_required(tmp_path: Path) -> None:
    assert _schema_codes(tmp_path, "---\ntype: doc\ndescription: D\n---\n\n# T\n") == ["E003"]
    assert _schema_codes(tmp_path, "---\ntype: doc\ntitle: T\n---\n\n# T\n") == ["E004"]


def test_tags_must_be_a_list_of_strings(tmp_path: Path) -> None:
    tags_object = "---\ntype: doc\ntitle: T\ndescription: D\ntags: {}\n---\n\n# T\n"
    assert _schema_codes(tmp_path, tags_object) == ["E005"]
    tags_int = "---\ntype: doc\ntitle: T\ndescription: D\ntags: [1]\n---\n\n# T\n"
    assert _schema_codes(tmp_path, tags_int) == ["E005"]


def test_forbidden_frontmatter_keys_are_reported(tmp_path: Path) -> None:
    text = "---\ntype: doc\ntitle: T\ndescription: D\nowner: x\n---\n\n# T\n"
    assert _schema_codes(tmp_path, text) == ["E006"]


def test_a_many_short_lines_node_hits_the_line_backstop(tmp_path: Path) -> None:
    path = tmp_path / "agent" / "docs" / "probe.meiva.okf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE + "x\n" * (gate.MAX_LINES + 50), encoding="utf-8")
    root = tmp_path.resolve()
    errors = gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)
    assert [e.code for e in errors] == ["E007"]
    assert "File too long" in errors[0].msg
    assert str(gate.MAX_LINES) in errors[0].msg


def test_find_files_skips_hidden_dirs_except_github(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.meiva.okf.md").write_text(_NODE, encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / ".github.meiva.okf.md").write_text(_NODE, encoding="utf-8")
    (tmp_path / "visible.meiva.okf.md").write_text(_NODE, encoding="utf-8")
    names = sorted(p.name for p in gate.find_files([str(tmp_path)]))
    assert names == [".github.meiva.okf.md", "visible.meiva.okf.md"]


def test_type_error_names_the_allowed_types_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "agent" / "docs" / "probe.meiva.okf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE.replace("type: doc", "type: page"), encoding="utf-8")
    root = tmp_path.resolve()
    messages = [e.msg for e in gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)]
    assert messages == ["type must be one of ['doc', 'memory'], got 'page'"]


def test_the_line_backstop_passes_at_the_cap_and_counts_real_lines(tmp_path: Path) -> None:
    node_dir = tmp_path / "agent" / "docs"
    node_dir.mkdir(parents=True, exist_ok=True)
    root = tmp_path.resolve()
    at_cap = node_dir / "at-cap.meiva.okf.md"
    at_cap.write_text(_NODE + "x\n" * (gate.MAX_LINES - len(_NODE.splitlines())), encoding="utf-8")
    assert gate.lint_file(at_cap.resolve(), gate.collect_all_paths(root), root) == []

    over = node_dir / "over-cap.meiva.okf.md"
    over.write_text(
        _NODE + "x\n" * (gate.MAX_LINES - len(_NODE.splitlines()) + 1), encoding="utf-8"
    )
    errors = gate.lint_file(over.resolve(), gate.collect_all_paths(root), root)
    assert [e.code for e in errors] == ["E007"]
    assert f"File too long: {gate.MAX_LINES + 1} lines (max {gate.MAX_LINES})" in errors[0].msg
