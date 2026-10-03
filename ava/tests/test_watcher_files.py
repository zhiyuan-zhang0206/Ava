"""ava.watcher generated-file housekeeping: per-agent script dir, stale pruning, session file lifetimes; split from ava/tests/test_watcher.py (task #4922)."""

from __future__ import annotations

import pathlib

import pytest

import ava
from ava import watcher
from base.native_process.os_platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); apply it
    # module-wide here since every watcher session test needs the fake-id +
    # pty cleanup isolation. `pty_service` first — the isolation fixture's
    # own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("pty_service", "_isolated_agent"),
]


def test_watcher_script_dir_is_tmp_per_agent(_agent_row: int) -> None:
    # Generated watcher scripts live under the system temp dir, scoped per
    # cluster + agent — NOT in $AVA_HOME (the old global `watchers/` dir there
    # accumulated 180+ files and let co-agents overwrite each other's scripts)
    # and NOT in the workspace. Session ids are per-agent counters, so a
    # per-agent subdir makes cross-agent collision impossible.
    import tempfile

    from base.paths import ava_home

    d = watcher._watchers_dir()
    td = pathlib.Path(tempfile.gettempdir())
    assert d.is_relative_to(td / "ava")  # under $TMPDIR/ava/<cluster>/<agent>/
    assert str(ava_home()) not in str(d)  # never under $AVA_HOME
    assert d.name == "watchers"
    # cluster segment: the home basename heads the per-cluster dir
    slug = ava_home().name.lstrip(".") or "cluster"
    assert any(part == slug or part.startswith(f"{slug}-") for part in d.parts)
    assert d.is_dir()


def test_spawn_prunes_stale_watcher_files(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every launch deletes generated watcher files from earlier watchers: a
    # hard-killed watcher cannot self-clean, and its pair would otherwise
    # accumulate forever. Non-generated files are left alone.
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    d = watcher._watchers_dir()
    stale_script = d / "watcher_999.py"
    stale_boot = d / "watcher_999_boot.py"
    stale_script.write_text("old")
    stale_boot.write_text("old boot")
    keep = d / "keep_me.py"
    keep.write_text("not generated")

    wid = watcher.launch("import ava\n", timeout="1h", name="test-prune")

    assert not stale_script.exists()
    assert not stale_boot.exists()
    assert keep.exists()  # non-generated files survive
    assert (d / f"watcher_{wid}.py").exists()  # the new pair is there
    assert (d / f"watcher_{wid}_boot.py").exists()


def test_prune_does_not_touch_other_agents_files(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolation is per-agent: pruning this agent's stale files must never
    # reach into another agent's subdir (the old global dir let agents
    # overwrite each other — that is the bug this layout exists to prevent).
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    d = watcher._watchers_dir()
    other = d.parent / str(ava.self.AGENT_ID + 1) / "watchers"
    other.mkdir(parents=True, exist_ok=True)
    foreign = other / "watcher_999.py"
    foreign.write_text("someone else's")

    watcher.launch("import ava\n", timeout="1h", name="test-isolation")

    assert foreign.exists()  # untouched


def test_spawn_keeps_sibling_files_while_session_alive(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug A (task #1116): a generated pair whose SESSION still exists must
    not be pruned — launch is asynchronous (the command is sent into a fresh
    session whose shell takes a moment to come up), so a back-to-back sibling
    launch deleting it would make that watcher's python start fail with
    "can't open file ... _boot.py". Only provably-dead pairs (session gone)
    are pruned."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", lambda: {4242: "test-sibling"})
    d = watcher._watchers_dir()
    live_script = d / "watcher_4242.py"
    live_boot = d / "watcher_4242_boot.py"
    live_script.write_text("still needed")
    live_boot.write_text("still needed boot")
    dead_script = d / "watcher_4243.py"
    dead_boot = d / "watcher_4243_boot.py"
    dead_script.write_text("dead")
    dead_boot.write_text("dead boot")

    wid = watcher.launch("import ava\n", timeout="1h", name="test-prune-live")

    # live sibling's pair survives (session 4242 exists); dead pair is pruned
    assert live_script.exists() and live_boot.exists()
    assert not dead_script.exists() and not dead_boot.exists()
    assert (d / f"watcher_{wid}.py").exists()


def test_spawn_back_to_back_keeps_all_files(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-to-back launches must keep every live sibling's boot file (task #1116).
    The fake exposes each session immediately, as the backend does."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    alive: set[int] = set()
    counter = iter(range(1000, 1003))

    def _fake_create(
        name: str, *, ttl: float, system: bool = False, env_overrides: dict[str, str] | None = None
    ) -> tuple[int, str]:
        sid = next(counter)
        alive.add(sid)
        return sid, name

    monkeypatch.setattr(_sessions, "create_session", _fake_create)
    monkeypatch.setattr(_sessions, "list", lambda: dict.fromkeys(alive, "w"))
    d = watcher._watchers_dir()

    ids: list[int] = []
    for name in ("b2b-1", "b2b-2", "b2b-3"):
        wid = watcher.launch("import ava\n", timeout="1h", name=name)
        ids.append(wid)

    # all three pairs on disk — none pruned while its session is live
    for wid in ids:
        assert (d / f"watcher_{wid}.py").exists(), f"watcher_{wid}.py pruned"
        assert (d / f"watcher_{wid}_boot.py").exists(), f"watcher_{wid}_boot.py pruned"
    assert len(ids) == 3 and len(set(ids)) == 3
