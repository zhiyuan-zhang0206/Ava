from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.host_ops import fleet_update_keep as keep

_INIT = (
    "base/__init__.py",
    "base/host/__init__.py",
    "base/events/__init__.py",
    "base/sessions/__init__.py",
    "base/sessions/pty/__init__.py",
    "services/__init__.py",
    "services/agent_runner/__init__.py",
    "services/agent_runner/pty_sessions/__init__.py",
    "services/desktop/__init__.py",
    "services/desktop/browser/__init__.py",
    "services/supervision/__init__.py",
    "services/supervision/healthchecks/__init__.py",
    "ava/__init__.py",
    "ava/mcps/__init__.py",
    "ava_builtins/__init__.py",
    "ava_builtins/mcps/__init__.py",
    "ava_builtins/mcps/chrome/__init__.py",
)
_FILES = {
    "base/host/atomic_io.py": "LEAF = 1\n",
    "base/sessions/pty/protocol.py": "from base.host.atomic_io import LEAF\n",
    "services/agent_runner/pty_sessions/daemon.py": "from base.sessions.pty.protocol import LEAF\n",
    "base/events/bus.py": "BUS = 1\n",
    "services/desktop/browser/daemon.py": "from base.events.bus import BUS\n",
    "ava/mcps/_browser.py": "WATCH = 1\n",
    "services/supervision/healthchecks/protocol_probe.py": "PROBE = 1\n",
    "services/supervision/healthchecks/browser.py": "BPROBE = 1\n",
    "services/supervision/healthchecks/browser_mcp.py": "MPROBE = 1\n",
    "ui/app.py": "APP = 1\n",
}


def _git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def _write(repo: Path, changes: Mapping[str, str | None]) -> None:
    for relative, content in changes.items():
        path = repo / relative
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def _commit(repo: Path, message: str, changes: Mapping[str, str | None]) -> str:
    _write(repo, changes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _decide(repo: Path, old: str, new: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert keep.main(["--repo", str(repo), "--old", old, "--new", new]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _verdict(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(component for component in payload["components"] if component["name"] == name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    for name in _INIT:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    _write(root, _FILES)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_no_change_keeps_everything(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    payload = _decide(repo, head, head, capsys)
    assert payload["changed_files"] == 0
    assert payload["keep_services"] == ["pty-sessions", "browser", "browser-mcp"]
    assert all(component["keep"] for component in payload["components"])
    assert all(component["surface"] is None for component in payload["components"])


def test_a_pty_change_restarts_only_pty(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(
        repo,
        "pty",
        {"base/sessions/pty/protocol.py": "from base.host.atomic_io import LEAF\nPTY = 2\n"},
    )
    payload = _decide(repo, base, new, capsys)
    assert payload["keep_services"] == ["browser", "browser-mcp"]
    pty = _verdict(payload, "pty-sessions")
    assert not pty["keep"] and pty["hits"] == ["base/sessions/pty/protocol.py"]
    assert _verdict(payload, "browser")["keep"]
    assert _git(repo, "worktree", "list").count("\n") == 0


def test_a_transitive_dependency_restarts_the_consumer(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "leaf", {"base/host/atomic_io.py": "LEAF = 2\n"})
    payload = _decide(repo, base, new, capsys)
    pty = _verdict(payload, "pty-sessions")
    assert not pty["keep"] and pty["hits"] == ["base/host/atomic_io.py"]
    assert _verdict(payload, "browser")["keep"]


def test_a_browser_dependency_restarts_the_browser(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "bus", {"base/events/bus.py": "BUS = 2\n"})
    payload = _decide(repo, base, new, capsys)
    assert payload["keep_services"] == ["pty-sessions"]
    browser = _verdict(payload, "browser")
    assert not browser["keep"] and browser["hits"] == ["base/events/bus.py"]
    assert _verdict(payload, "pty-sessions")["keep"]


@pytest.mark.parametrize(
    ("watched", "component"),
    [
        ("ava/mcps/_browser.py", "browser"),
        ("services/supervision/healthchecks/browser.py", "browser"),
        ("services/supervision/healthchecks/browser_mcp.py", "browser"),
        ("services/supervision/healthchecks/protocol_probe.py", "pty-sessions"),
    ],
)
def test_a_watch_file_restarts_its_component(
    repo: Path, capsys: pytest.CaptureFixture[str], watched: str, component: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "watch", {watched: "WATCH = 2\n"})
    payload = _decide(repo, base, new, capsys)
    verdict = _verdict(payload, component)
    assert not verdict["keep"] and verdict["hits"] == [watched]
    other = _verdict(payload, "browser" if component == "pty-sessions" else "pty-sessions")
    assert other["keep"]


def test_an_unrelated_change_keeps_both(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "ui", {"ui/app.py": "APP = 2\n"})
    payload = _decide(repo, base, new, capsys)
    assert payload["keep_services"] == ["pty-sessions", "browser", "browser-mcp"]
    assert all(component["hit_count"] == 0 for component in payload["components"])


def test_tests_and_docs_under_a_component_are_not_roots(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(
        repo,
        "docs and tests",
        {
            "services/desktop/browser/tests/test_x.py": "T = 1\n",
            "services/desktop/browser/docs/notes.md": "D = 1\n",
        },
    )
    payload = _decide(repo, base, new, capsys)
    assert payload["changed_files"] == 2
    assert payload["keep_services"] == ["pty-sessions", "browser", "browser-mcp"]


def test_a_deleted_surface_file_restarts_its_component(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "delete", {"base/sessions/pty/protocol.py": None})
    payload = _decide(repo, base, new, capsys)
    pty = _verdict(payload, "pty-sessions")
    assert not pty["keep"] and pty["hits"] == ["base/sessions/pty/protocol.py"]
    assert _verdict(payload, "browser")["keep"]


def test_an_added_file_under_a_component_dir_restarts_it(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    new = _commit(repo, "add", {"services/desktop/browser/extra.py": "EXTRA = 1\n"})
    payload = _decide(repo, base, new, capsys)
    browser = _verdict(payload, "browser")
    assert not browser["keep"] and browser["hits"] == ["services/desktop/browser/extra.py"]


def test_a_component_with_no_matching_root_restarts_conservatively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "other"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _write(root, {"services/desktop/browser/daemon.py": "B = 1\n", "README.md": "R = 1\n"})
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    new = _commit(root, "readme", {"README.md": "R = 2\n"})
    payload = _decide(root, base, new, capsys)
    pty = _verdict(payload, "pty-sessions")
    assert not pty["keep"] and "no root file" in pty["reason"]
    browser = _verdict(payload, "browser")
    assert not browser["keep"] and "watch files absent at both revisions" in browser["reason"]
    assert payload["keep_services"] == []


def test_a_watch_file_absent_at_both_revisions_restarts_conservatively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "partial"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    for name in _INIT:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    _write(root, {name: text for name, text in _FILES.items() if name != "ava/mcps/_browser.py"})
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    new = _commit(root, "ui", {"ui/app.py": "APP = 2\n"})
    payload = _decide(root, base, new, capsys)
    browser = _verdict(payload, "browser")
    assert not browser["keep"]
    assert browser["reason"] == (
        "watch files absent at both revisions: ava/mcps/_browser.py; restarting"
    )
    assert _verdict(payload, "pty-sessions")["keep"]


@pytest.mark.parametrize("ref", ["deadbeefdeadbeef", "HEAD~99"])
def test_a_bad_revision_fails(ref: str, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    assert keep.main(["--repo", str(repo), "--old", ref, "--new", head]) == 1
    assert "not a commit" in capsys.readouterr().err
    assert keep.main(["--repo", str(repo), "--old", head, "--new", ref]) == 1
    assert "not a commit" in capsys.readouterr().err


def test_the_closure_follows_chains_and_survives_cycles() -> None:
    adjacency = {"a": {"b", "c"}, "b": {"c"}, "c": {"a"}}
    assert keep.closure(adjacency, ["a"]) == {"a", "b", "c"}


def test_a_surface_directory_covers_everything_below_it() -> None:
    surface = frozenset({"services/desktop/browser", "base/events/bus.py"})
    changed = ["services/desktop/browser/x/y.py", "base/events/bus.py", "ui/app.py"]
    assert keep.hit_files(surface, changed) == [
        "services/desktop/browser/x/y.py",
        "base/events/bus.py",
    ]


def test_roots_at_skips_tests_and_docs_and_watch_files() -> None:
    files = frozenset(
        {
            "base/sessions/pty/protocol.py",
            "base/sessions/pty/tests/test_protocol.py",
            "base/sessions/pty/docs/notes.md",
            "services/supervision/healthchecks/protocol_probe.py",
            "ui/app.py",
        }
    )
    component = keep.COMPONENTS[0]
    assert keep.roots_at(files, component) == ["base/sessions/pty/protocol.py"]
