"""Unit tests for `ava.files.read / write / append / edit / glob / delete`.

Use pytest's `tmp_path` fixture — each test gets an independent directory, automatically cleaned.

Relative path behavior: does not depend on system cwd; by default resolves to the agent workspace
(`$AVA_HOME/workspaces/<agent_id>`; conftest globally binds `_agent_id = 1`).
When identity is not bound (pre-bootstrap, `_agent_id = None`), resolves to `$HOME`.
`~/...` goes through expanduser and always points to $HOME. Absolute paths are unchanged.

workspace mock: shared `workspace` fixture (tests/conftest.py) explicitly pins
`_agent_id=1` and points `settings.general.ava_home` to tmp_path, returns the resolution base
`<tmp>/workspaces/1` (workspace_dir is created on demand, not pre-built).

$HOME mock premise: `Path.home()` goes through `os.path.expanduser("~")` which uses the `$HOME` env
(CPython current behavior). Pre-identity tests use `patch.dict(os.environ, {"HOME": ...})`
to switch $HOME, paired with `assert Path.home() == ...` to lock that the mock is effective —
if someday stdlib behavior changes (e.g. sandbox doesn't read $HOME) this assertion will blow up,
indicating the mock path needs to be adjusted.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import ava
import ava._boot

# Permission tests are ineffective when run as root (root bypasses all fs permissions). CI container
# defaults to root, local dev / prod is non-root.
_skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses fs perms — permission tests no-op as root"
)


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate pre-bootstrap process (conftest's global `_agent_id = 1` is removed),
    relative path falls back to `$HOME` resolution."""
    monkeypatch.setattr(ava._boot, "_agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)


# ── read / write / delete basics ───────────────────────────────────────────


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    ava.files.write(str(p), "你好 world\n")
    assert ava.files.read(str(p)) == "你好 world\n"


def test_read_returns_full_content(tmp_path: Path) -> None:
    """read does not truncate — large files returned verbatim. The overall stdout
    amount is controlled by exec_node's envelope truncation (exec_output_max_chars)
    managed at a single layer, from the agent's perspective only one layer of truncation."""
    p = tmp_path / "big.txt"
    content = "x" * 25_000
    ava.files.write(str(p), content)

    got = ava.files.read(str(p))
    assert got == content
    assert len(got) == 25_000


def test_read_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ava.files.read(str(tmp_path / "nope.txt"))


# ── read line ranges (start / end, 1-indexed inclusive) ────────────────────────


def _five_line_file(tmp_path: Path) -> Path:
    p = tmp_path / "lines.txt"
    p.write_text("one\ntwo\nthree\nfour\nfive\n")
    return p


def test_read_start_only(tmp_path: Path) -> None:
    """start=3 → read from line 3 to end, 1-indexed."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=3) == "three\nfour\nfive\n"


def test_read_end_only(tmp_path: Path) -> None:
    """end=2 → read first 2 lines, inclusive."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), end=2) == "one\ntwo\n"


def test_read_start_and_end(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=2, end=4) == "two\nthree\nfour\n"


def test_read_single_line(tmp_path: Path) -> None:
    """start == end → exactly that line (with newline)."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=3, end=3) == "three\n"


def test_read_no_range_args_returns_full_file(tmp_path: Path) -> None:
    """Both args None is equivalent to not passing them, takes the original full-content path."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p)) == ava.files.read(str(p), start=None, end=None)


def test_read_start_past_eof_returns_empty(tmp_path: Path) -> None:
    """start beyond total lines → returns empty string (mirrors sed -n 'N,$p')."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=99) == ""


def test_read_end_past_eof_clamps(tmp_path: Path) -> None:
    """end beyond total lines → clamps to the end (mirrors sed)."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=4, end=99) == "four\nfive\n"


def test_read_last_line_no_trailing_newline(tmp_path: Path) -> None:
    """When the last line has no \\n, range read should preserve the original bytes (no trailing newline)."""
    p = tmp_path / "no_eol.txt"
    p.write_text("a\nb\nc")  # no trailing newline
    assert ava.files.read(str(p), start=3, end=3) == "c"
    assert ava.files.read(str(p), start=2) == "b\nc"


def test_read_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("")
    assert ava.files.read(str(p), start=1) == ""
    assert ava.files.read(str(p), start=1, end=100) == ""


def test_read_start_zero_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match="start must be >= 1"):
        ava.files.read(str(p), start=0)


def test_read_end_zero_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match="end must be >= 1"):
        ava.files.read(str(p), end=0)


def test_read_negative_start_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match="start must be >= 1"):
        ava.files.read(str(p), start=-5)


def test_read_start_after_end_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match=r"start \(5\) must be <= end \(3\)"):
        ava.files.read(str(p), start=5, end=3)


# --- limit (Claude Code-style pagination) ---


def test_read_limit_only(tmp_path: Path) -> None:
    """limit=2 with no start returns the first 2 lines."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), limit=2) == "one\ntwo\n"


def test_read_start_and_limit(tmp_path: Path) -> None:
    """start + limit: take `limit` lines starting at `start` (1-indexed)."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=2, limit=2) == "two\nthree\n"


def test_read_limit_past_eof_clamps(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=4, limit=100) == "four\nfive\n"


def test_read_limit_zero_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        ava.files.read(str(p), limit=0)


def test_read_mixing_limit_and_end_raises(tmp_path: Path) -> None:
    p = _five_line_file(tmp_path)
    with pytest.raises(ValueError, match="either `end` or `limit`"):
        ava.files.read(str(p), end=3, limit=2)


# --- with_line_numbers ---


def test_read_with_line_numbers_full_file(tmp_path: Path) -> None:
    """Full file with line numbers prefixed; width matches highest line no."""
    p = _five_line_file(tmp_path)
    expected = "1: one\n2: two\n3: three\n4: four\n5: five\n"
    assert ava.files.read(str(p), with_line_numbers=True) == expected


def test_read_with_line_numbers_range(tmp_path: Path) -> None:
    """Line numbers reflect original-file position, not slice index."""
    p = _five_line_file(tmp_path)
    assert (
        ava.files.read(str(p), start=3, end=5, with_line_numbers=True)
        == "3: three\n4: four\n5: five\n"
    )


def test_read_with_line_numbers_width_pads(tmp_path: Path) -> None:
    """For files with >9 lines, line numbers right-pad to the max width."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "".join(f"L{i}\n" for i in range(1, 13)))
    out = ava.files.read(str(p), start=8, end=12, with_line_numbers=True)
    assert out == " 8: L8\n 9: L9\n10: L10\n11: L11\n12: L12\n"


def test_read_with_line_numbers_start_limit(tmp_path: Path) -> None:
    """with_line_numbers composes with start + limit."""
    p = _five_line_file(tmp_path)
    assert ava.files.read(str(p), start=3, limit=2, with_line_numbers=True) == "3: three\n4: four\n"


def test_write_creates_missing_parents(tmp_path: Path) -> None:
    """parent dir missing → recursively mkdir then write (aligned with unix `mkdir -p` + write file),
    saving cross-machine hand-offs where the target directory hasn't been created yet causing write failures."""
    target = tmp_path / "a" / "b" / "c" / "f.txt"
    assert not target.parent.exists()
    ava.files.write(str(target), "x")
    assert target.read_text() == "x"


def test_write_directory_target_raises(tmp_path: Path) -> None:
    """`write(dir, ...)` → `IsADirectoryError` (Path.write_text behavior). Prevents silent overwrite."""
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        ava.files.write(str(d), "x")


def test_delete_directory_target_raises(tmp_path: Path) -> None:
    """`delete(dir)` should not silently delete a dir — but the specific exception IsADirectoryError
    (Linux) vs PermissionError (macOS) is inconsistent across OS (POSIX unlink(2) EISDIR vs EPERM).
    `OSError` is the parent of both; catching that is sufficient — the core invariant is "do not
    silently delete dir"."""
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(OSError):
        ava.files.delete(str(d))
    assert d.exists()  # confirm dir was not deleted


def test_delete_removes_file(tmp_path: Path) -> None:
    p = tmp_path / "rm.txt"
    ava.files.write(str(p), "x")
    assert p.exists()
    ava.files.delete(str(p))
    assert not p.exists()


def test_delete_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ava.files.delete(str(tmp_path / "nope.txt"))


# ── relative path based on workspace behavior ─────────────────────────────────────────


def test_relative_read_resolves_to_workspace(workspace: Path) -> None:
    """`read("foo.txt")` → reads `<workspace>/foo.txt`, unrelated to system cwd."""
    workspace.mkdir(parents=True)
    (workspace / "foo.txt").write_text("ws content")
    assert ava.files.read("foo.txt") == "ws content"


def test_relative_write_resolves_to_workspace(workspace: Path) -> None:
    """`write("foo.txt", ...)` → writes to `<workspace>/foo.txt` (directory created on demand)."""
    ava.files.write("hello.txt", "hi")
    assert (workspace / "hello.txt").read_text() == "hi"


def test_relative_delete_resolves_to_workspace(workspace: Path) -> None:
    """`delete("foo.txt")` → deletes `<workspace>/foo.txt`."""
    workspace.mkdir(parents=True)
    (workspace / "rm.txt").write_text("")
    ava.files.delete("rm.txt")
    assert not (workspace / "rm.txt").exists()


def test_relative_resolves_to_home_before_identity(no_identity: None, tmp_path: Path) -> None:
    """Identity not bound (pre-bootstrap) → relative path falls back to `$HOME` resolution."""
    (tmp_path / "foo.txt").write_text("home content")
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert Path.home() == tmp_path  # mock lock: Path.home() truly uses $HOME env
        assert ava.files.read("foo.txt") == "home content"
        ava.files.write("out.txt", "hi")
    assert (tmp_path / "out.txt").read_text() == "hi"


def test_tilde_expanduser(tmp_path: Path) -> None:
    """`~/foo.txt` goes through expanduser → `$HOME/foo.txt` — tilde always points to home,
    unaffected by workspace resolution."""
    (tmp_path / "tilde.txt").write_text("via tilde")
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert Path.home() == tmp_path  # mock lock
        assert ava.files.read("~/tilde.txt") == "via tilde"


def test_system_cwd_does_not_affect_relative_path(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative path always goes to workspace, independent of system cwd.

    Differential test: `<workspace>/config.txt` and `other_cwd/config.txt` have different content,
    switch system cwd to `other_cwd`, verify `read("config.txt")` gets the workspace copy.
    """
    workspace.mkdir(parents=True)
    (workspace / "config.txt").write_text("from-workspace")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    (other_cwd / "config.txt").write_text("from-other-cwd")

    monkeypatch.chdir(other_cwd)  # system cwd = other_cwd
    assert ava.files.read("config.txt") == "from-workspace"


def test_absolute_path_unaffected_by_workspace(workspace: Path, tmp_path: Path) -> None:
    """Absolute paths are unchanged — passing `/full/path` reads that path, unrelated to workspace."""
    p = tmp_path / "abs.txt"
    p.write_text("abs content")
    assert ava.files.read(str(p)) == "abs content"
    assert not (workspace / "abs.txt").exists()


# ── nested / binary / dir-target ─────────────────────────────────────────


def test_nested_relative_path_resolves_to_workspace(workspace: Path) -> None:
    """Nested relative path: `sub/foo.txt` → `<workspace>/sub/foo.txt`."""
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "foo.txt").write_text("nested")
    assert ava.files.read("sub/foo.txt") == "nested"


def test_nested_relative_missing_parent_creates_tree(workspace: Path) -> None:
    """When parent dir of a nested relative path (`<workspace>/sub`) does not exist, `write` recursively
    creates `<workspace>/sub` then writes — relative path also uses auto-mkdir."""
    # intentionally do not pre-create workspace itself, write recursively creates it too
    ava.files.write("sub/foo.txt", "x")
    assert (workspace / "sub" / "foo.txt").read_text() == "x"


def test_read_binary_raises_unicode_decode_error(tmp_path: Path) -> None:
    """Binary file not utf-8, read should raise `UnicodeDecodeError` instead of silently
    returning replacement chars — the agent clearly knows "this is not text"."""
    p = tmp_path / "binary.bin"
    p.write_bytes(b"\xff\xfe\x00\x01\x02non-utf-8")
    with pytest.raises(UnicodeDecodeError):
        ava.files.read(str(p))


def test_read_directory_raises(tmp_path: Path) -> None:
    """`read(dir)` → `IsADirectoryError` (Path.read_text behavior, clearly documented)."""
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        ava.files.read(str(d))


# ── append ───────────────────────────────────────────────────────────────


def test_append_to_existing_file(tmp_path: Path) -> None:
    """append does not overwrite; content is added to the end."""
    p = tmp_path / "log.txt"
    ava.files.write(str(p), "line1\n")
    ava.files.append(str(p), "line2\n")
    ava.files.append(str(p), "line3\n")
    assert ava.files.read(str(p)) == "line1\nline2\nline3\n"


def test_append_auto_creates_file(tmp_path: Path) -> None:
    """Aligned with unix `>>` behavior: file doesn't exist → auto-create then append. Consistent
    with write behavior (both auto-create file)."""
    p = tmp_path / "new.txt"
    assert not p.exists()
    ava.files.append(str(p), "first content\n")
    assert ava.files.read(str(p)) == "first content\n"


def test_append_creates_missing_parents(tmp_path: Path) -> None:
    """parent dir missing → recursively mkdir then append (consistent with write, both auto-mkdir)."""
    target = tmp_path / "a" / "b" / "c" / "log.txt"
    assert not target.parent.exists()
    ava.files.append(str(target), "x")
    assert target.read_text() == "x"


def test_append_directory_target_raises(tmp_path: Path) -> None:
    """`append(dir, ...)` → IsADirectoryError (open("a", <dir>) behavior)."""
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        ava.files.append(str(d), "x")


def test_append_relative_resolves_to_workspace(workspace: Path) -> None:
    """`append("foo.txt", ...)` → writes to `<workspace>/foo.txt`."""
    ava.files.write("hello.txt", "hi\n")
    ava.files.append("hello.txt", "more\n")
    assert (workspace / "hello.txt").read_text() == "hi\nmore\n"


def test_append_utf8_encoding(tmp_path: Path) -> None:
    """append uses utf-8 (consistent with read/write)."""
    p = tmp_path / "cn.txt"
    ava.files.write(str(p), "你好")
    ava.files.append(str(p), " 世界")
    assert ava.files.read(str(p)) == "你好 世界"


# ── edit ────────────────────────────────────────────────────────────────


def test_edit_single_replace(tmp_path: Path) -> None:
    """edit replaces a single occurrence of old, other content unchanged."""
    p = tmp_path / "code.py"
    ava.files.write(str(p), "x = 1\ny = 2\nz = 3\n")
    ava.files.edit(str(p), "y = 2", "y = 200")
    assert ava.files.read(str(p)) == "x = 1\ny = 200\nz = 3\n"


def test_edit_multiline_replace(tmp_path: Path) -> None:
    """edit supports multiline old / new."""
    p = tmp_path / "code.py"
    ava.files.write(str(p), "before\nfoo\nbar\nafter\n")
    ava.files.edit(str(p), "foo\nbar", "BAZ")
    assert ava.files.read(str(p)) == "before\nBAZ\nafter\n"


def test_edit_old_not_found_raises(tmp_path: Path) -> None:
    """old not found → ValueError (fail-fast, no silent no-op)."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "hello\n")
    with pytest.raises(ValueError, match="old not found"):
        ava.files.edit(str(p), "nonexistent", "new")
    assert ava.files.read(str(p)) == "hello\n"  # file unchanged


def test_edit_old_not_found_includes_fuzzy_hint(tmp_path: Path) -> None:
    """Near-miss (whitespace drift) → error message includes closest match + diff."""
    p = tmp_path / "f.txt"
    ava.files.write(
        str(p),
        "def foo():\n    return self._cache[key]\n\n\ndef bar():\n    return None\n",
    )
    # `old` differs from the file by extra spaces inside `[ key ]`
    with pytest.raises(ValueError) as excinfo:
        ava.files.edit(
            str(p),
            old="def foo():\n    return self._cache[ key ]\n",
            new="def foo():\n    return self._cache.get(key)\n",
        )
    msg = str(excinfo.value)
    assert "old not found" in msg
    assert "Closest match at lines" in msg
    # Diff should mention the actual line's whitespace
    assert "[key]" in msg


def test_edit_old_not_found_no_hint_when_nothing_close(tmp_path: Path) -> None:
    """No remotely-similar window → error stays clean (no misleading hint)."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "completely unrelated content\n")
    with pytest.raises(ValueError) as excinfo:
        ava.files.edit(str(p), old="from xyz import abc\nresult = abc()\n", new="x")
    assert "Closest match" not in str(excinfo.value)


def test_edit_old_not_found_old_longer_than_file(tmp_path: Path) -> None:
    """`old` larger than the file → no hint (no possible window)."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "one line\n")
    with pytest.raises(ValueError) as excinfo:
        ava.files.edit(str(p), old="line A\nline B\nline C\n", new="x")
    assert "Closest match" not in str(excinfo.value)


def test_edit_multi_match_without_replace_all_raises(tmp_path: Path) -> None:
    """old appears multiple times, replace_all=False → ValueError (requires explicit)."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "x\nx\nx\n")
    with pytest.raises(ValueError, match="appears 3 times"):
        ava.files.edit(str(p), "x", "y")
    assert ava.files.read(str(p)) == "x\nx\nx\n"  # file unchanged


def test_edit_replace_all(tmp_path: Path) -> None:
    """replace_all=True replaces all occurrences."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "x\nx\nx\n")
    ava.files.edit(str(p), "x", "y", replace_all=True)
    assert ava.files.read(str(p)) == "y\ny\ny\n"


def test_edit_replace_all_zero_match_still_raises(tmp_path: Path) -> None:
    """replace_all=True but old appears 0 times → still raise (fail-fast)."""
    p = tmp_path / "f.txt"
    ava.files.write(str(p), "hello\n")
    with pytest.raises(ValueError, match="old not found"):
        ava.files.edit(str(p), "missing", "new", replace_all=True)


def test_edit_missing_file_raises(tmp_path: Path) -> None:
    """edit on non-existent file → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        ava.files.edit(str(tmp_path / "nope.txt"), "a", "b")


def test_edit_directory_target_raises(tmp_path: Path) -> None:
    """`edit(dir, ...)` → IsADirectoryError (read_text on dir)."""
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        ava.files.edit(str(d), "a", "b")


def test_edit_relative_resolves_to_workspace(workspace: Path) -> None:
    """`edit("foo.txt", ...)` → edits `<workspace>/foo.txt`."""
    ava.files.write("f.txt", "alpha\n")
    ava.files.edit("f.txt", "alpha", "beta")
    assert (workspace / "f.txt").read_text() == "beta\n"


# ── glob ────────────────────────────────────────────────────────────────


def test_glob_basic_star(workspace: Path) -> None:
    """`glob("*.txt")` lists all .txt files under workspace."""
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("")
    (workspace / "b.txt").write_text("")
    (workspace / "c.md").write_text("")
    got = ava.files.glob("*.txt")
    assert [p.name for p in got] == ["a.txt", "b.txt"]


def test_glob_recursive(workspace: Path) -> None:
    """`**` recursive: lists matching files in subdirectories."""
    workspace.mkdir(parents=True)
    (workspace / "x.md").write_text("")
    sub = workspace / "sub"
    sub.mkdir()
    (sub / "y.md").write_text("")
    nested = sub / "deep"
    nested.mkdir()
    (nested / "z.md").write_text("")
    got = ava.files.glob("**/*.md")
    names = [p.name for p in got]
    assert "x.md" in names
    assert "y.md" in names
    assert "z.md" in names


def test_glob_no_match_returns_empty(workspace: Path) -> None:
    """pattern doesn't match → returns empty list, no exception thrown (unlike read/write, glob tolerates empty)."""
    assert ava.files.glob("*.nonexistent") == []


def test_glob_absolute_pattern(tmp_path: Path) -> None:
    """Absolute path pattern does not go through $HOME."""
    (tmp_path / "a.txt").write_text("")
    (tmp_path / "b.txt").write_text("")
    got = ava.files.glob(str(tmp_path / "*.txt"))
    assert sorted(p.name for p in got) == ["a.txt", "b.txt"]


def test_glob_returns_sorted_paths(workspace: Path) -> None:
    """Returned list is lexicographically sorted (deterministic)."""
    workspace.mkdir(parents=True)
    for name in ["zebra.md", "alpha.md", "midnight.md"]:
        (workspace / name).write_text("")
    got = ava.files.glob("*.md")
    assert [p.name for p in got] == ["alpha.md", "midnight.md", "zebra.md"]


def test_glob_returns_path_objects(workspace: Path) -> None:
    """Returns `Path` not str — agent can use .name / .parent / .suffix / pass to read."""
    workspace.mkdir(parents=True)
    (workspace / "x.txt").write_text("content")
    got = ava.files.glob("x.txt")
    assert len(got) == 1
    assert isinstance(got[0], Path)
    assert got[0].is_absolute()
    assert ava.files.read(str(got[0])) == "content"


# ── PermissionError (cross-function) ──────────────────────────────────────────────


@_skip_if_root
def test_read_permission_denied_raises(tmp_path: Path) -> None:
    """file mode 000 → PermissionError."""
    p = tmp_path / "locked.txt"
    p.write_text("secret")
    p.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            ava.files.read(str(p))
    finally:
        p.chmod(0o644)


@_skip_if_root
def test_write_permission_denied_raises(tmp_path: Path) -> None:
    """parent dir with no write permission → PermissionError. Uses a readonly parent dir instead
    of chmod file (Path.write_text's overwrite path first unlinks then opens, triggering permission
    at different times)."""
    d = tmp_path / "readonly_dir"
    d.mkdir()
    d.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            ava.files.write(str(d / "new.txt"), "x")
    finally:
        d.chmod(0o755)


@_skip_if_root
def test_append_permission_denied_raises(tmp_path: Path) -> None:
    """parent dir with no write permission → PermissionError (append on new file needs create perm)."""
    d = tmp_path / "readonly_dir"
    d.mkdir()
    d.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            ava.files.append(str(d / "new.txt"), "x")
    finally:
        d.chmod(0o755)


@_skip_if_root
def test_edit_permission_denied_raises(tmp_path: Path) -> None:
    """edit write stage with file lacking write permission → PermissionError (file mode 0o444)."""
    p = tmp_path / "f.txt"
    p.write_text("hello")
    p.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            ava.files.edit(str(p), "hello", "world")
    finally:
        p.chmod(0o644)


@_skip_if_root
def test_delete_permission_denied_raises(tmp_path: Path) -> None:
    """parent dir with no write permission → PermissionError (unlink needs parent w)."""
    d = tmp_path / "readonly_dir"
    d.mkdir()
    target = d / "victim.txt"
    target.write_text("x")
    d.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            ava.files.delete(str(target))
    finally:
        d.chmod(0o755)
