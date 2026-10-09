"""`plugins.ava_code` integration tests — ava.cwd namespace, files.read and ui.serve wraps,
AGENTS.md injection dedup, end to end: the surface installed from `plugin.contribute()`, state declared
through `agent_runtime.contribute()`.
"""

import io
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

import ava
from agent.state import BaseAgentState, CompactState, build_agent_state
from ava.sdk_surface import install
from ava_builtins.plugins.ava_code.tests.surface_support import code_registry
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import (
    ExtensionRegistry,
)
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _load_ava_code_plugin(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog, config_authority: ConfigAuthority
):
    """Install ava_code's declared SDK surface (cwd namespace, wraps, skill source) for each
    test and uninstall it after. Use a valid local sampling policy for SDK calls."""
    from base.agents.sdk import call_policy

    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    install.install(code_registry(), catalog=model_catalog, authority=config_authority)

    yield

    install.uninstall()


def _make_state_with_cwd(
    cwd: str,
    *,
    injected: set[str] | None = None,
    last_seen_compact: int = 0,
    compact_version: int = 0,
) -> BaseAgentState:
    """Build dynamic AgentState instance filling cwd + optional dedup state + optional compact counter."""
    from ava_builtins.plugins.ava_code import agent_runtime

    state_cls = build_agent_state(ExtensionRegistry((("ava_code", agent_runtime.contribute()),)))
    kwargs: dict = {"ava_code__cwd": cwd, "ava_code__last_seen_compact": last_seen_compact}
    if injected is not None:
        kwargs["ava_code__injected_paths"] = injected
    if compact_version:
        kwargs["compact"] = CompactState(version=compact_version)
    return state_cls(messages=[], halted=False, **kwargs)  # pyright: ignore[reportUnknownArgumentType]


def _get_injected_context_notes(state_update: dict[str, object]) -> list[dict[str, str]]:
    """Extract CONTEXT system notes from the exec's messages delta.

    The plugin delivers AGENTS.md / CLAUDE.md notes in-memory through the
    base `messages` channel of `ava.state_update` (user ruling 2026-08-11:
    no side-channel file). Each note's content is
    "Project <fname> from <path>:\n\n<body>"."""
    from typing import cast

    from langchain_core.messages import AnyMessage

    from agent.messages import read_ava_kwargs
    from base.agents.messages.kwargs import AvaMsgType, NoteTag

    notes: list[dict[str, str]] = []
    for msg in cast(list[AnyMessage], state_update.get("messages", [])):
        kw = read_ava_kwargs(msg)
        if (
            kw.get("ava_msg_type") == AvaMsgType.SYSTEM_NOTE.value
            and kw.get("ava_note_tag") == NoteTag.CONTEXT.value
        ):
            # system_note_message prepends "[system] " to the content
            content = msg.content  # pyright: ignore[reportUnknownMemberType]
            assert isinstance(content, str)
            assert content.startswith("[system] ")
            head, _, body = content[len("[system] ") :].partition("\n\n")
            notes.append({"prefix": head, "content": body})
    return notes


# ── ava.cwd.get / set ─────────────────────────────────────────────────────


def test_get_cwd_returns_state_value(tmp_path: Path):
    """get_cwd reads from ava.state.ava_code.cwd, returns Path."""
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        result = ava.cwd.get()
        assert result == Path(str(tmp_path))
        assert isinstance(result, Path)
    finally:
        ava.unbind_exec_turn()


def test_files_module_docstring_keeps_core_path_claim(_load_ava_code_plugin):
    """The core owns the overview, including the optional coding directory."""
    assert ava.files.__doc__ is not None
    assert "workspace by default" in ava.files.__doc__
    assert "`ava.cwd` when available" in ava.files.__doc__
    assert "tracked working directory" not in ava.files.__doc__


def test_get_cwd_after_set_cwd_reads_new_value_without_changing_process_cwd(tmp_path: Path):
    """Within one turn the logical state changes immediately, while the
    Python process cwd remains an infrastructure property."""
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.mkdir()
    p2.mkdir()

    ava.state = _make_state_with_cwd(str(p1))
    ava.state_update = {}
    process_cwd = Path.cwd()
    try:
        ava.cwd.set(p2)
        assert ava.cwd.get() == p2.resolve()
        assert Path.cwd() == process_cwd
    finally:
        ava.unbind_exec_turn()


def test_get_cwd_outside_turn_raises():
    """Outside an exec turn there is no ava.state → PluginStateOutsideTurnError."""
    assert not ava.in_exec_turn()
    with pytest.raises(ava.PluginStateOutsideTurnError):
        ava.cwd.get()


def test_default_cwd_is_workspace_when_bootstrapped(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """In bootstrapped process, cwd default = own workspace dir (and already created)."""
    from ava_builtins.plugins.ava_code._state import default_cwd

    pin_agent(5, owns_loop=True)
    assert default_cwd() == str(unit_home / "workspaces" / "5")
    assert (unit_home / "workspaces" / "5").is_dir()


def test_default_cwd_home_without_bootstrap(monkeypatch: pytest.MonkeyPatch):
    """No process identity (test/REPL directly construct state) → keep $HOME placeholder behavior."""
    from ava_builtins.plugins.ava_code._state import default_cwd

    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert default_cwd() == str(Path.home())


def test_set_cwd_writes_state_update(tmp_path: Path):
    """set_cwd writes new path into state_update["ava_code__cwd"]."""
    ava.state = _make_state_with_cwd(str(Path.home()))
    ava.state_update = {}
    try:
        ava.cwd.set(tmp_path)
        assert ava.state_update["ava_code__cwd"] == str(tmp_path.resolve())
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_nonexistent_raises(tmp_path: Path):
    """path does not exist → FileNotFoundError, state_update unchanged."""
    fake = tmp_path / "no-such-dir"
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        with pytest.raises(FileNotFoundError):
            ava.cwd.set(fake)
        assert "ava_code__cwd" not in ava.state_update
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_not_directory_raises(tmp_path: Path):
    """path exists but is not directory → NotADirectoryError."""
    f = tmp_path / "file.txt"
    f.write_text("")
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        with pytest.raises(NotADirectoryError):
            ava.cwd.set(f)
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_outside_turn_raises(tmp_path: Path):
    """Outside exec turn set_cwd → PluginStateOutsideTurnError."""
    assert not ava.in_exec_turn()
    with pytest.raises(ava.PluginStateOutsideTurnError):
        ava.cwd.set(tmp_path)


# ── files.read wrap: AGENTS.md injection ──────────────────────────────────────


def _make_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_read_wrap_injects_agents_md(tmp_path: Path):
    """ava.files.read("foo.py") walks up cwd path looking for AGENTS.md and
    appends its content as a CONTEXT system note to the exec's messages delta
    (in-memory — no side-channel file)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("PROJECT CONVENTIONS")
    target = repo / "foo.py"
    target.write_text("# code")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            content = ava.files.read("foo.py")

        assert content == "# code"
        # The note rides the exec's messages delta, not a file / stdout
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "PROJECT CONVENTIONS" in notes[0]["content"]
        agents_path = str((repo / "AGENTS.md").resolve())
        assert f"Project AGENTS.md from {agents_path}:" == notes[0]["prefix"]
        # injected_paths adds this AGENTS.md
        assert agents_path in ava.state_update["ava_code__injected_paths"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_injects_agents_and_claude(tmp_path: Path):
    """Same directory has both AGENTS.md and CLAUDE.md → both injected, each with its own prefix (path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("AGENTS CONVENTIONS")
    (repo / "CLAUDE.md").write_text("CLAUDE CONVENTIONS")
    (repo / "foo.py").write_text("# code")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")

        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 2
        agents_path = str((repo / "AGENTS.md").resolve())
        claude_path = str((repo / "CLAUDE.md").resolve())
        assert "AGENTS CONVENTIONS" in notes[0]["content"]
        assert "CLAUDE CONVENTIONS" in notes[1]["content"]
        assert f"Project AGENTS.md from {agents_path}:" == notes[0]["prefix"]
        assert f"Project CLAUDE.md from {claude_path}:" == notes[1]["prefix"]
        injected = ava.state_update["ava_code__injected_paths"]
        assert agents_path in injected
        assert claude_path in injected
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_no_emoji_in_marker(tmp_path: Path):
    """marker without emoji — plain `Project <file> from <path>:` text."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    (repo / "foo.py").write_text("# code")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ava.files.read("foo.py")
        assert "📎" not in buf.getvalue()  # emoji-ok: asserts the marker is emoji-free
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_dedup_via_injected_paths(tmp_path: Path):
    """AGENTS.md already recorded in injected_paths, sibling file read does not re-inject."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")
    (repo / "foo.py").write_text("")

    ava.state = _make_state_with_cwd(str(repo), injected={str(agents_md.resolve())})
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ava.files.read("foo.py")

        assert "PROJECT" not in buf.getvalue()
        assert "Project AGENTS.md from" not in buf.getvalue()
        # injected_paths unchanged → doesn't write update
        assert "ava_code__injected_paths" not in ava.state_update
    finally:
        ava.unbind_exec_turn()


# ── files.read wrap: line-range params (start/end/limit/with_line_numbers) ──
# The wrapper must forward the paging params to the underlying read at BOTH
# call sites (the outside-a-turn fast-path AND the in-turn cwd-resolved path).
# Plain ava.files unit tests import the unwrapped function, so they never
# exercise the wrapper — these tests are the coverage that the agent-facing
# ava.files.read actually accepts the params.


def test_read_wrap_forwards_line_range_fast_path(tmp_path: Path):
    """Outside a turn (not in_exec_turn, fast-path), the wrapper forwards
    start/end/limit/with_line_numbers to the underlying read."""
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\nthree\nfour\nfive\n")
    assert not ava.in_exec_turn()  # plugin loaded by autouse fixture, but no active turn
    assert ava.files.read(str(p), start=2, end=3) == "two\nthree\n"
    assert ava.files.read(str(p), start=2, limit=2) == "two\nthree\n"
    assert ava.files.read(str(p), start=3, with_line_numbers=True) == "3: three\n4: four\n5: five\n"
    # Default (path only) is byte-identical to the full file.
    assert ava.files.read(str(p)) == "one\ntwo\nthree\nfour\nfive\n"


def test_read_wrap_forwards_line_range_in_turn(tmp_path: Path):
    """In-turn (ava.state set), the wrapper forwards the paging params through
    the cwd-resolved read so agents can page large files / get line numbers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            assert ava.files.read("f.txt", start=2, end=3) == "two\nthree\n"
            assert ava.files.read("f.txt", start=2, limit=2) == "two\nthree\n"
            assert (
                ava.files.read("f.txt", start=3, with_line_numbers=True)
                == "3: three\n4: four\n5: five\n"
            )
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_target_is_agents_md_marks_but_not_prints(tmp_path: Path):
    """agent directly reads AGENTS.md (primary path) → content returned as value, no longer print marker
    (which would double content in messages); also mark into injected_paths so sibling read
    no longer auto-inject.

    This is ava_code's "fallback" design semantics: wrap only helps surface when agent hasn't actively read,
    steps aside after active read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                content = ava.files.read("AGENTS.md")

        assert content == "PROJECT"  # content returned as value
        assert (
            "Project AGENTS.md from" not in buf.getvalue()
        )  # no longer print marker (avoid double surface)
        assert str(agents_md.resolve()) in ava.state_update["ava_code__injected_paths"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_target_is_claude_md_marks_but_not_prints(tmp_path: Path):
    """agent directly reads CLAUDE.md (primary path) → same as AGENTS.md: content returns as value,
    no re-print marker, but marks into injected_paths. Verify that samefile suppression also works for the second filename."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    claude_md = repo / "CLAUDE.md"
    claude_md.write_text("CLAUDE PROJECT")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                content = ava.files.read("CLAUDE.md")

        assert content == "CLAUDE PROJECT"  # content returned as value
        assert "Project CLAUDE.md from" not in buf.getvalue()  # no re-print marker
        assert str(claude_md.resolve()) in ava.state_update["ava_code__injected_paths"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_target_agents_md_via_symlink_still_primary(tmp_path: Path):
    """agent reads AGENTS.md through symlink → samefile recognizes same inode, primary path hit,
    no re-print marker. Lock case-insensitive FS / symlink / hardlink behavior — `==` path comparison
    would fail due to different path strings causing double-surface, samefile compares inode so doesn't."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")
    link = repo / "agents-link.md"
    link.symlink_to(agents_md)

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                # read via symlink — target resolves to AGENTS.md, should recognize as primary
                content = ava.files.read("agents-link.md")
        assert content == "PROJECT"
        # primary path → no print marker (content already returned as value)
        assert "Project AGENTS.md from" not in buf.getvalue()
        # injected_paths adds AGENTS.md (resolved real path)
        assert str(agents_md.resolve()) in ava.state_update["ava_code__injected_paths"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_target_agents_md_then_sibling_no_reinject(tmp_path: Path):
    """primary path (agent directly reads AGENTS.md) → subsequent sibling file read no longer auto-injects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")
    (repo / "foo.py").write_text("# code")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ava.files.read("AGENTS.md")  # mark
                ava.files.read("foo.py")  # no re-inject

        # whole stdout has no marker — AGENTS.md.read doesn't print, foo.py.read sees
        # injected_paths already has this AGENTS.md → skip auto-inject
        assert "Project AGENTS.md from" not in buf.getvalue()
        assert "PROJECT" not in buf.getvalue()
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_compact_resets_injected_paths(tmp_path: Path):
    """compact.version grows past ava_code's last_seen_compact →
    wrap entry lazy clears injected_paths, letting AGENTS.md re-surface to agent.

    The monotonic version-counter reset pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")
    (repo / "foo.py").write_text("")

    # previous turn already injected (last_seen_compact=0); but compact has run once → now version=1
    ava.state = _make_state_with_cwd(
        str(repo),
        injected={str(agents_md.resolve())},
        last_seen_compact=0,
        compact_version=1,
    )
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")

        # injected_paths reset → AGENTS.md re-injected into the messages delta
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "PROJECT" in notes[0]["content"]
        assert ava.state_update["ava_code__last_seen_compact"] == 1
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_compact_not_advanced_keeps_dedup(tmp_path: Path):
    """compact.version in sync with bookmark (e.g. already processed by this plugin) → no reset."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_md = repo / "AGENTS.md"
    agents_md.write_text("PROJECT")
    (repo / "foo.py").write_text("")

    ava.state = _make_state_with_cwd(
        str(repo),
        injected={str(agents_md.resolve())},
        last_seen_compact=1,
        compact_version=1,
    )
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ava.files.read("foo.py")

        # bookmark == compact.version → no reset, injected_paths preserved,
        # no re-injection into the messages delta
        assert "Project AGENTS.md from" not in buf.getvalue()
        assert "ava_code__injected_paths" not in ava.state_update
        assert "ava_code__last_seen_compact" not in ava.state_update
        assert "messages" not in ava.state_update
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_dedup_within_same_turn(tmp_path: Path):
    """Multiple reads of different sibling files within same turn, same AGENTS.md injected only once.

    state_handle.update synchronously mutates ava.state working copy, next handle.read()
    immediately sees injected_paths update — no extra turn cache needed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("PROJECT")
    (repo / "foo.py").write_text("")
    (repo / "bar.py").write_text("")

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")
            ava.files.read("bar.py")

        # same AGENTS.md only injected once
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "PROJECT" in notes[0]["content"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_resolves_relative_to_cwd(tmp_path: Path):
    """Relative path resolved via ava.cwd maintained cwd — not system cwd."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    (repo / "foo.py").write_text("# code")

    # system cwd unchanged, plugin cwd set to repo
    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            # using relative path
            content = ava.files.read("foo.py")
        assert content == "# code"
        # AGENTS.md injected (plugin uses plugin cwd to resolve path)
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "X" in notes[0]["content"]
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_no_agents_md_no_op(tmp_path: Path):
    """target path neither in git repo nor under $HOME → walk returns [], no print."""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    target = isolated / "foo.py"
    target.write_text("# code")

    ava.state = _make_state_with_cwd(str(isolated))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ava.files.read(str(target))
        assert buf.getvalue() == ""  # no injection
        assert "messages" not in ava.state_update
    finally:
        ava.unbind_exec_turn()


# ── review-fix guard tests (I8/I9/I10) ──────────────────────────────────


def test_read_wrap_outside_turn_passthrough(tmp_path: Path):
    """not `ava.in_exec_turn()` (outside turn / test / dev) → wrap fast-path passthrough to original read,
    does not change path or inject. Aligned with plugin disabled behavior — avoid silent fallback to weird cwd.

    Use absolute path to verify "original read is actually called" — `ava.files.read` itself resolves relative paths
    based on $HOME (since PR #194), but this test aims to verify wrap fast-path passthrough, not
    the underlying read path behavior, so use absolute path to separate the two concerns.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    target = repo / "foo.py"
    target.write_text("hi")

    assert not ava.in_exec_turn()
    buf = io.StringIO()
    with redirect_stdout(buf):
        content = ava.files.read(str(target))
    assert content == "hi"
    assert buf.getvalue() == ""  # no injection (wrap fast-path passthrough)


def test_read_wrap_target_missing_still_injects(tmp_path: Path):
    """target doesn't exist → _orig_read raises FileNotFoundError, but AGENTS.md walk +
    inject runs before _orig_read → marker still recorded.

    documented behavior: AGENTS.md inject is best-effort surface to agent, does not depend
    on target actually being readable. Test locks current behavior; future refactor that wants to change to "inject only after read success"
    would need to invert this test.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    # don't create foo.py

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            with pytest.raises(FileNotFoundError):
                ava.files.read("foo.py")
        # target doesn't exist, but AGENTS.md walk + inject runs before _orig_read
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "X" in notes[0]["content"]
        agents_path = str((repo / "AGENTS.md").resolve())
        assert f"Project AGENTS.md from {agents_path}:" == notes[0]["prefix"]
    finally:
        ava.unbind_exec_turn()


# ── ava.ui.serve wrap ─────────────────────────────────────────────────────


# ── project-local skill source (set_cwd surfaces + records) ────────────────


# ── coding tools section: dedup vs the framework's expanded-SDK section ─────


# ── ava.understand wrap (paths follow the tracked cwd) ────────────────────


def _stub_understand_text_path(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the text-path provider so wrap tests never hit a real model."""
    from unittest.mock import MagicMock

    llm = MagicMock()
    response = MagicMock()
    response.content = "ok"
    response.response_metadata = {}
    llm.invoke.return_value = response
    captured: dict = {"llm": llm}
    monkeypatch.setattr("base.lm.factory.build_chat_model", lambda _model, **_kw: llm)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return captured


# ── hash-based dedup ─────────────────────────────────────────────────────


# ── project-skills note injection ──────────────────────────────────────────


# ── persisted-cwd validation after init ──────────────────────────────────


# ── oversized context file: truncate + archive (user ruling 2026-08-11) ─────


def test_read_wrap_oversized_agents_md_truncates_and_archives(tmp_path: Path, monkeypatch):
    """A context file over exec_output_max_chars is injected truncated
    (head + tail) with the full text archived to the workspace .exec_output/
    ring — same logic as exec output overflow; the archive path rides in the
    note so the agent can read / grep the complete content."""
    from base.config import settings
    from base.paths import workspace_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    big = ("HEAD_MARKER" + ("x" * 5000) + "TAIL_MARKER").encode()
    (repo / "AGENTS.md").write_bytes(big)
    (repo / "foo.py").write_text("# code")

    monkeypatch.setattr(settings.sandbox, "exec_output_max_chars", 500)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    pin_agent(123)
    overflow = workspace_dir(123) / ".exec_output"

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")
        notes = _get_injected_context_notes(ava.state_update)
        assert len(notes) == 1
        assert "HEAD_MARKER" in notes[0]["content"], "head must survive"
        assert "TAIL_MARKER" in notes[0]["content"], "tail must survive"
        assert "output truncated" in notes[0]["content"]
        # full content archived, path reported in the note
        files = list(overflow.glob("exec_*.txt"))
        assert len(files) == 1
        assert str(files[0]) in notes[0]["content"]
        assert files[0].read_bytes() == big
    finally:
        ava.unbind_exec_turn()
