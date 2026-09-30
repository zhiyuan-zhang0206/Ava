"""`scripts/content_lint/lint_ava_okf.py` — gate-level behavior.

A typo'd explicit target must fail the gate: an explicit path argument that
does not exist must report the missing target on stderr and exit 1, not print
"No .ava.okf.md files found." and exit 0.

E009 (overview position) is judged on the logical path, so a node in a
package's `docs/` layer is checked as if it sat where the layer sits.
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


def _e009(repo_root: Path, rel: str) -> list[str]:
    """The E009 messages the gate reports for `rel` inside the repo `repo_root`."""
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE, encoding="utf-8")
    root = repo_root.resolve()
    errors = gate.lint_file(path.resolve(), gate.collect_all_paths(root), root)
    return [e.msg for e in errors if e.code == "E009"]


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
