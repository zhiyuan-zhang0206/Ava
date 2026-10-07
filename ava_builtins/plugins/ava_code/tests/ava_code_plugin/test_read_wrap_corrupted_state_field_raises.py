"""Ava code plugin cases: read wrap corrupted state field raises."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

import ava
from ava.sdk_surface import install
from ava_builtins.plugins.ava_code.tests.surface_support import code_registry
from ava_builtins.plugins.ava_code.tests.test_ava_code_plugin import (
    _get_injected_context_notes,
    _make_git_repo,
    _make_state_with_cwd,
    _stub_understand_text_path,
)
from ava_builtins.plugins.ava_code.tests.test_ava_code_plugin import (
    _load_ava_code_plugin as _load_ava_code_plugin,
)
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import PluginContributions, SdkNamespace, SdkWrap


def test_read_wrap_corrupted_state_field_raises(tmp_path: Path):
    """sibling mutates ava.state.ava_code__injected_paths to non-valid type → handle.read()
    Pydantic validation raises (handle.read goes through cls.model_validate, schema type error explodes on the spot)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    (repo / "foo.py").write_text("")

    ava.state = _make_state_with_cwd(str(repo))
    # pyright type=ignore: deliberately mutate field to wrong type, simulating sibling plugin / framework bug.
    ava.state.ava_code__injected_paths = 42  # type: ignore[assignment]
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            with pytest.raises(Exception, match=r"injected_paths"):
                ava.files.read("foo.py")
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_relative_resolves_against_current_cwd(tmp_path: Path):
    """set_cwd accepts relative path → resolve against current get_cwd()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sub = repo / "src"
    sub.mkdir()

    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        ava.cwd.set("src")  # relative path
        assert ava.state_update["ava_code__cwd"] == str(sub.resolve())
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_expanduser_supported(tmp_path: Path):
    """set_cwd accepts `~/...` uses expanduser, resolves via $HOME."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    sub = fake_home / "project"
    sub.mkdir()

    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        ava.state = _make_state_with_cwd(str(tmp_path))
        ava.state_update = {}
        try:
            ava.cwd.set("~/project")
            assert ava.state_update["ava_code__cwd"] == str(sub.resolve())
        finally:
            ava.unbind_exec_turn()


def test_uninstall_restores_original(tmp_path: Path):
    """uninstall restores every wrapped target to its captured original and removes the
    plugin's namespace.

    The chained callable presents as the function it replaced — same `__name__` /
    `__module__` as the original — so "is it wrapped" is a registry question
    (`ava.extend.stack`), not a `__module__` sniff. After uninstall the registry is empty and
    the namespace holds a different object (the original).
    """
    from ava.sdk_surface import wraps

    # fixture already installed ava_code -> files.read carries one wrap layer
    assert wraps.stack("files.read")  # non-empty: wrapped
    wrapped = ava.files.read

    install.uninstall()
    assert wraps.stack("files.read") == []  # registry emptied
    assert ava.files.read is not wrapped  # restored to the original object
    assert ava.files.read.__module__ == "ava.files"
    assert not hasattr(ava, "cwd")


def test_duplicate_namespace_refuses_the_second_plugin_whole():
    """A second plugin declaring `cwd` is refused and rolled back whole: its earlier namespace and
    its wrap are not installed, and ava_code's own surface is untouched."""
    from ava_builtins.plugins.ava_code import _code_namespace

    def _other_wrap(inner: Callable[..., object], path: str, *args: object, **kwargs: object):
        return inner(path, *args, **kwargs)

    install.uninstall()
    other = PluginContributions(
        sdk_namespaces=(
            SdkNamespace("other_ns", _code_namespace),
            SdkNamespace("cwd", _code_namespace),
        ),
        sdk_wraps=(SdkWrap("files.read", _other_wrap),),
    )
    admitted = install.install(code_registry(("other_plugin", other)))

    assert [name for name, _ in admitted.plugins] == ["ava_code"]
    assert not hasattr(ava, "other_ns")
    assert [p for p, _ in ava.extend.stack("files.read")] == ["ava_code"]


def test_second_wrap_stacks_instead_of_asserting():
    """A second wrapper on ava.files.read stacks on top of ava_code's, in
    deterministic registration order — the retired exclusivity assert used to
    reject this. `ava.extend.stack` shows both layers; the dedup that used to
    justify the assert now holds because ava_code calls `inner` exactly once
    regardless of how many layers sit below."""
    # fixture already installed ava_code -> layer 1 on files.read
    before = ava.extend.stack("files.read")
    assert [p for p, _ in before] == ["ava_code"]

    def _external_wrap(inner: Callable[..., object], path: str, *args: object, **kwargs: object):
        return inner(path, *args, **kwargs)

    install.uninstall()
    install.install(
        code_registry(
            (
                "other_plugin",
                PluginContributions(sdk_wraps=(SdkWrap("files.read", _external_wrap),)),
            )
        )
    )

    after = ava.extend.stack("files.read")
    assert [p for p, _ in after] == ["ava_code", "other_plugin"]  # stacked, no assert


def test_plugin_wraps_all_files_ops_for_cwd():
    """plugin ava_code wraps all ava.files.* operations (+ shell.run / understand) —
    not just read.

    read additionally handles AGENTS.md auto-injection; write / append / edit / delete / glob
    only do cwd path resolution (no AGENTS.md injection), ensuring that after agent ava.cwd.set(), all
    file operation paths are consistent. Use introspection via `ava.extend.stack` to verify each target
    is wrapped by ava_code — the chained wrapper masquerades __module__ as the original, so a
    `__module__ != "ava.files"` probe fails; wrap fact lives in registry.
    """
    # fixture already installed ava_code plugin
    for target in (
        "files.read",
        "files.write",
        "files.append",
        "files.edit",
        "files.glob",
        "files.delete",
        "shell.run",
        "understand",
    ):
        stack = ava.extend.stack(target)
        assert [p for p, _ in stack] == ["ava_code"], target


def test_shell_run_wrap_passes_logical_cwd_without_changing_process_cwd(tmp_path: Path):
    """The shell wrapper passes logical state as an explicit subprocess cwd;
    it never relies on or mutates the Python process cwd."""
    from ava_builtins.plugins.ava_code.plugin import _wrapped_shell_run

    logical_cwd = tmp_path / "logical"
    logical_cwd.mkdir()
    process_cwd = Path.cwd()
    captured: dict[str, object] = {}

    def fake_inner(cmd: str, *, cwd: str | None, timeout: float) -> str:
        captured.update(cmd=cmd, cwd=cwd, timeout=timeout)
        return "ok"

    ava.state = _make_state_with_cwd(str(logical_cwd))
    ava.state_update = {}
    try:
        assert _wrapped_shell_run(fake_inner, "pwd", timeout=7.0) == "ok"
        assert captured == {"cmd": "pwd", "cwd": str(logical_cwd), "timeout": 7.0}
        assert Path.cwd() == process_cwd
    finally:
        ava.unbind_exec_turn()


def test_serve_wrap_resolves_relative_dir_against_cwd(tmp_path: Path):
    from ava_builtins.plugins.ava_code.plugin import _wrapped_serve

    captured: dict[str, object] = {}

    def fake_inner(
        dir: str,
        name: str,
        port: int | None = None,
        title: str | None = None,
        *,
        ttl: float | None = None,
    ) -> str:
        captured.update(dir=dir, name=name, port=port, title=title, ttl=ttl)
        return "served"

    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        assert _wrapped_serve(fake_inner, "site", "preview", port=8123, title="Preview") == "served"
        assert captured == {
            "dir": str((tmp_path / "site").resolve()),
            "name": "preview",
            "port": 8123,
            "title": "Preview",
            "ttl": None,
        }
    finally:
        ava.unbind_exec_turn()


def test_serve_wrap_passes_absolute_dir_through(tmp_path: Path):
    from ava_builtins.plugins.ava_code.plugin import _wrapped_serve

    captured: dict[str, object] = {}

    def fake_inner(
        dir: str,
        name: str,
        port: int | None = None,
        title: str | None = None,
        *,
        ttl: float | None = None,
    ) -> str:
        captured.update(dir=dir, name=name, port=port, title=title, ttl=ttl)
        return "served"

    logical_cwd = tmp_path / "cwd"
    logical_cwd.mkdir()
    absolute_dir = tmp_path / "site"
    ava.state = _make_state_with_cwd(str(logical_cwd))
    ava.state_update = {}
    try:
        assert _wrapped_serve(fake_inner, str(absolute_dir), "preview") == "served"
        assert captured["dir"] == str(absolute_dir.resolve())
    finally:
        ava.unbind_exec_turn()


def test_serve_wrap_registered():
    assert ava.extend.stack("ui.serve")


def test_set_cwd_surfaces_and_stores_cwd_note(tmp_path: Path):
    """set_cwd into a repo with `.agents/skills/` surfaces those skills under
    `ava.skills.*` (via the provider the plugin registered) and stores a
    cwd_note for the after-exec hook to inject as a system reminder."""
    import ava.skills as ava_skills

    repo = tmp_path / "repo"
    demo = repo / ".ava" / "skills" / "demo-proj"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo-proj\ndescription: a project-local demo\n---\nbody",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        ava.cwd.set(repo)
        # No print output — cwd_note is set in state for the after-exec hook
        assert ava.state.ava_code__cwd_note is not None  # type: ignore[union-attr]
        assert f"Working directory set to {repo}" in ava.state.ava_code__cwd_note  # type: ignore[union-attr]
        assert "demo-proj" in {s["name"] for s in ava_skills.names()}
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_non_git_stores_cwd_note(tmp_path: Path):
    """A cwd not under a git repo sets cwd_note with just the path
    (no project-skills listing)."""
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        ava.cwd.set(tmp_path)
        # No print output — cwd_note is set in state
        note = ava.state.ava_code__cwd_note  # type: ignore[union-attr]
        assert note is not None
        assert f"Working directory set to {tmp_path}" == note
    finally:
        ava.unbind_exec_turn()


def test_coding_tools_section_skips_framework_expanded_modules(monkeypatch: pytest.MonkeyPatch):
    """A module in the effective expand view (settings + plugin registrations)
    is already rendered (full contract) by the framework section — the plugin
    must not promote it a second time. Exact path match: an expand entry for a
    child (e.g. `shell.sessions`) does not suppress the parent's stub. `cwd`
    is declared as an expanded namespace by `contribute()`, so it is
    always expanded and never promoted here."""
    from ava_builtins.plugins.ava_code._prompt_sections import _coding_tools_section
    from base.config import settings

    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["files", "shell.sessions"])
    text = _coding_tools_section(AgentSlices.resolve())
    assert "## ava.files" not in text  # expanded by the framework -> skipped
    assert "## ava.shell" in text  # only the child is expanded -> parent stays
    assert "## ava.cwd" not in text  # plugin-registered expand -> skipped too
    assert "Use the Ava file and shell tools" in text  # preamble always renders


def test_coding_tools_section_all_expanded_keeps_preamble_only(
    monkeypatch: pytest.MonkeyPatch,
):
    from ava_builtins.plugins.ava_code._prompt_sections import _coding_tools_section
    from base.config import settings

    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["cwd", "files", "shell"])
    text = _coding_tools_section(AgentSlices.resolve())
    assert text.startswith("# Coding tools")
    assert "Use the Ava file and shell tools" in text
    # search steering: rg over recursive grep/find (recursive grep times out on
    # the worktree-heavy checkout) — guard for the user-reported slowness
    assert "`rg` (ripgrep)" in text
    assert "## ava." not in text
    assert not text.endswith("\n\n")


def test_understand_wrap_resolves_paths_against_cwd(tmp_path: Path, monkeypatch):
    """In-turn relative paths= resolved against tracked cwd — same string same file as files.read."""
    captured = _stub_understand_text_path(monkeypatch)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    (tmp_path / "rel.txt").write_text("cwd material", encoding="utf-8")
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        [out] = ava.understand([{"prompt": "p", "paths": ["rel.txt"]}])
        assert out == "ok"
        sent = captured["llm"].invoke.call_args[0][0][0].content  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        assert sent[0] == {"type": "text", "text": "cwd material"}
    finally:
        ava.unbind_exec_turn()


def test_understand_wrap_missing_paths_raises_with_cwd_location(tmp_path: Path):
    """In-turn relative paths= that does not exist under cwd → FileNotFoundError points to cwd-resolved result
    (same semantics as files.read, won't silently treat as text)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ava.state = _make_state_with_cwd(str(repo))
    ava.state_update = {}
    try:
        with pytest.raises(FileNotFoundError, match=r"nope\.txt"):
            ava.understand([{"prompt": "p", "paths": ["nope.txt"]}])
    finally:
        ava.unbind_exec_turn()


def test_understand_wrap_outside_turn_defers_to_workspace(workspace: Path, monkeypatch):
    """Outside turn (not in_exec_turn) → workspace baseline resolution (via _resolve_for_cwd passthrough)."""
    captured = _stub_understand_text_path(monkeypatch)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert not ava.in_exec_turn()
    workspace.mkdir(parents=True)
    (workspace / "f.txt").write_text("ws material", encoding="utf-8")
    ava.understand([{"prompt": "p", "paths": ["f.txt"]}])
    sent = captured["llm"].invoke.call_args[0][0][0].content  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert sent[0] == {"type": "text", "text": "ws material"}


def test_understand_wrap_keeps_error_attribute_and_doc():
    """After wrap, ava.understand.UnderstandError still reachable (agent's documented catch path),
    docstring original contract preserved."""
    import importlib

    understand_mod = importlib.import_module("ava.understand")

    assert ava.understand.UnderstandError is understand_mod.UnderstandError  # type: ignore[attr-defined] # pyright: ignore[reportFunctionMemberAccess]
    assert ava.understand.__doc__ == understand_mod.understand.__doc__


def test_understand_wrap_passes_invalid_combo_to_core(tmp_path: Path):
    """A malformed target reaches the core untouched so its canonical ValueError
    fires — the wrap does not preempt validation, in either direction (a target
    carrying both `paths` and `text`, or neither)."""
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        with pytest.raises(ValueError, match="exactly one"):
            ava.understand([{"prompt": "p"}])
        with pytest.raises(ValueError, match="exactly one"):
            ava.understand([{"prompt": "p", "paths": ["rel.txt"], "text": "t"}])
        # A `paths` value that is not a list of path strings also reaches the
        # core untouched, so its canonical TypeError fires (not a wrap error).
        with pytest.raises(TypeError, match="must be a list of file paths"):
            ava.understand([{"prompt": "p", "paths": "rel.txt"}])
    finally:
        ava.unbind_exec_turn()


def test_understand_wrap_forwards_effort(monkeypatch):
    """The effort knob reaches the core through the wrap: the default (max) and
    an explicit value both arrive at build_chat_model(reasoning_effort=...), and
    the advertised signature exposes the parameter."""
    import inspect
    from unittest.mock import MagicMock

    llm = MagicMock()
    response = MagicMock()
    response.content = "ok"
    response.response_metadata = {}
    llm.invoke.return_value = response
    efforts: list = []

    def _fake_build(_model: str, **kw):
        efforts.append(kw.get("reasoning_effort"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return llm

    monkeypatch.setattr("base.lm.factory.build_chat_model", _fake_build)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    ava.understand([{"prompt": "p", "text": "t"}])
    ava.understand([{"prompt": "p", "text": "t"}], effort="low")
    assert efforts == ["max", "low"]
    assert "effort" in inspect.signature(ava.understand).parameters


def test_understand_wrap_resolves_every_target_in_a_batch(tmp_path: Path, monkeypatch):
    """The wrap walks the whole batch: each `paths` target is resolved against the
    tracked cwd, while a `text` target passes through untouched."""
    from unittest.mock import MagicMock

    captured = _stub_understand_text_path(monkeypatch)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    materials: list[str] = []

    def _record(messages):
        materials.append(messages[0].content[0]["text"])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        response = MagicMock()
        response.content = "ok"
        response.response_metadata = {}
        return response

    captured["llm"].invoke.side_effect = _record  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    (tmp_path / "a.txt").write_text("material A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("material B", encoding="utf-8")
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        out = ava.understand(
            [
                {"prompt": "p1", "paths": ["a.txt"]},
                {"prompt": "p2", "text": "inline"},
                {"prompt": "p3", "paths": ["b.txt"]},
            ]
        )
    finally:
        ava.unbind_exec_turn()
    assert out == ["ok", "ok", "ok"]
    assert sorted(materials) == ["inline", "material A", "material B"]


def test_understand_wrap_resolves_every_entry_in_a_paths_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each entry of a multi-file `paths` target is resolved against the tracked
    cwd — the wrap walks the list, not just the first file."""
    captured = _stub_understand_text_path(monkeypatch)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    (tmp_path / "a.txt").write_text("material A", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("material B", encoding="utf-8")
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        [out] = ava.understand([{"prompt": "p", "paths": ["a.txt", "sub/b.txt"]}])
        assert out == "ok"
        sent = captured["llm"].invoke.call_args[0][0][0].content  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        assert sent[0] == {"type": "text", "text": "material A"}
        assert sent[1] == {"type": "text", "text": "material B"}
        assert sent[2] == {"type": "text", "text": "p"}
    finally:
        ava.unbind_exec_turn()


def test_understand_wrap_forwards_max_concurrent(tmp_path: Path, monkeypatch):
    """`max_concurrent` passes through the wrap to the core — a call with the
    knob completes, and the core's own validation still fires on a bad value
    (regression: the wrap used to drop the keyword and raise TypeError)."""
    _stub_understand_text_path(monkeypatch)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    (tmp_path / "rel.txt").write_text("cwd material", encoding="utf-8")
    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        [out] = ava.understand([{"prompt": "p", "paths": ["rel.txt"]}], max_concurrent=2)
        assert out == "ok"
        with pytest.raises(ValueError, match="at least 1"):
            ava.understand([{"prompt": "p", "paths": ["rel.txt"]}], max_concurrent=0)
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_dedup_by_content_hash_across_paths(tmp_path: Path):
    """Two AGENTS.md at different paths with identical content → only the first
    is auto-injected; the second is skipped by content-hash dedup. This is the
    worktree case: the worktree copy and the main repo copy share the same
    content but live at different absolute paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    # Main repo AGENTS.md
    agents_main = repo / "AGENTS.md"
    agents_main.write_text("SHARED CONTENT")
    # Worktree AGENTS.md (different path, same content)
    wt = repo / ".worktrees" / "wt1"
    wt.mkdir(parents=True)
    agents_wt = wt / "AGENTS.md"
    agents_wt.write_text("SHARED CONTENT")
    # A file deeper in the worktree — triggers walk across both AGENTS.md
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("# code")

    ava.state = _make_state_with_cwd(str(wt / "src"))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")

        notes = _get_injected_context_notes(ava.state_update)
        # Only ONE injection — the second AGENTS.md has the same content hash
        assert len(notes) == 1
        assert "SHARED CONTENT" in notes[0]["content"]
        # Both paths are marked in injected_paths (the second skipped but still
        # marked so path-based check short-circuits next time)
        injected = ava.state_update["ava_code__injected_paths"]
        assert str(agents_wt.resolve()) in injected
        assert str(agents_main.resolve()) in injected
        # The hash set records the single hash
        assert "ava_code__injected_hashes" in ava.state_update
        assert len(ava.state_update["ava_code__injected_hashes"]) == 1
    finally:
        ava.unbind_exec_turn()


def test_set_cwd_with_skills_stores_project_skills_note(tmp_path: Path):
    """set_cwd into a repo with project skills stores a summary string in
    project_skills_note for the after-exec hook to inject as a system note."""
    repo = tmp_path / "repo"
    demo = repo / ".ava" / "skills" / "demo-proj"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo-proj\ndescription: a project-local demo\n---\nbody",
        encoding="utf-8",
    )
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    ava.state = _make_state_with_cwd(str(tmp_path))
    ava.state_update = {}
    try:
        ava.cwd.set(repo)
        note = ava.state.ava_code__project_skills_note  # type: ignore[union-attr]
        assert note is not None
        assert "Skills available in this repo" in note
        assert "demo-proj" in note
        assert "a project-local demo" in note
    finally:
        ava.unbind_exec_turn()


def test_read_wrap_hash_dedup_respects_different_content(tmp_path: Path):
    """Two AGENTS.md at different paths with different content → both are
    injected. Hash dedup must not collapse distinct files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    agents_main = repo / "AGENTS.md"
    agents_main.write_text("MAIN CONTENT")
    wt = repo / ".worktrees" / "wt1"
    wt.mkdir(parents=True)
    agents_wt = wt / "AGENTS.md"
    agents_wt.write_text("WORKTREE CONTENT")
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("# code")

    ava.state = _make_state_with_cwd(str(wt / "src"))
    ava.state_update = {}
    try:
        with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
            (tmp_path / "fake-home").mkdir()
            ava.files.read("foo.py")

        notes = _get_injected_context_notes(ava.state_update)
        # Both injected — content differs
        assert len(notes) == 2
        contents = {e["content"] for e in notes}
        assert "MAIN CONTENT" in contents
        assert "WORKTREE CONTENT" in contents
        assert len(ava.state_update["ava_code__injected_hashes"]) == 2
    finally:
        ava.unbind_exec_turn()
