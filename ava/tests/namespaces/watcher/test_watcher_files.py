from __future__ import annotations

import pathlib
from collections.abc import Callable

import pytest

import ava
from ava.watcher import files, wiring

pytestmark = [
    pytest.mark.usefixtures("pty_service", "_isolated_agent"),
]


def _ignore_send(_session_id: int, _line: str) -> None:
    pass


def _watchers_dir() -> pathlib.Path:
    return wiring.spawn_deps().watchers_dir


def test_watcher_script_dir_is_tmp_per_agent(_agent_row: int) -> None:
    import tempfile

    from base.paths import ava_home

    d = _watchers_dir()
    td = pathlib.Path(tempfile.gettempdir())
    assert d.is_relative_to(td / "ava")
    assert str(ava_home()) not in str(d)
    assert d.name == "watchers"
    slug = ava_home().name.lstrip(".") or "cluster"
    assert any(part == slug or part.startswith(f"{slug}-") for part in d.parts)
    assert d.is_dir()


def test_spawn_prunes_stale_watcher_files(
    _agent_row: int, launch_watcher: Callable[..., int]
) -> None:
    d = _watchers_dir()
    stale_script = d / "watcher_999.py"
    stale_boot = d / "watcher_999_boot.py"
    stale_script.write_text("old")
    stale_boot.write_text("old boot")
    stale_launch = d / "watcher_999_launch.sh"
    stale_launch.write_text("old launch")
    keep = d / "keep_me.py"
    keep.write_text("not generated")

    wid = launch_watcher("import ava\n", "1h", "test-prune", send=_ignore_send)

    assert not stale_script.exists()
    assert not stale_boot.exists()
    assert not stale_launch.exists()
    assert keep.exists()
    assert (d / f"watcher_{wid}.py").exists()
    assert (d / f"watcher_{wid}_boot.py").exists()


def test_prune_does_not_touch_other_agents_files(
    _agent_row: int, launch_watcher: Callable[..., int]
) -> None:
    d = _watchers_dir()
    other = d.parent / str(ava.self.AGENT_ID + 1) / "watchers"
    other.mkdir(parents=True, exist_ok=True)
    foreign = other / "watcher_999.py"
    foreign.write_text("someone else's")

    launch_watcher("import ava\n", "1h", "test-isolation", send=_ignore_send)

    assert foreign.exists()


def test_spawn_keeps_sibling_files_while_session_alive(
    _agent_row: int, launch_watcher: Callable[..., int]
) -> None:
    d = _watchers_dir()
    live_script = d / "watcher_4242.py"
    live_boot = d / "watcher_4242_boot.py"
    live_script.write_text("still needed")
    live_boot.write_text("still needed boot")
    live_launch = d / "watcher_4242_launch.sh"
    live_launch.write_text("still needed launch")
    dead_script = d / "watcher_4243.py"
    dead_boot = d / "watcher_4243_boot.py"
    dead_script.write_text("dead")
    dead_boot.write_text("dead boot")
    dead_launch = d / "watcher_4243_launch.sh"
    dead_launch.write_text("dead launch")

    wid = launch_watcher(
        "import ava\n",
        "1h",
        "test-prune-live",
        send=_ignore_send,
        live_session_ids=lambda: {4242},
    )

    assert live_script.exists() and live_boot.exists() and live_launch.exists()
    assert not dead_script.exists() and not dead_boot.exists() and not dead_launch.exists()
    assert (d / f"watcher_{wid}.py").exists()


def test_spawn_back_to_back_keeps_all_files(
    _agent_row: int, launch_watcher: Callable[..., int]
) -> None:
    alive: set[int] = set()
    counter = iter(range(1000, 1003))

    def _fake_create(
        name: str, *, ttl: float, system: bool = False, env_overrides: dict[str, str] | None = None
    ) -> tuple[int, str]:
        sid = next(counter)
        alive.add(sid)
        return sid, name

    d = _watchers_dir()

    ids: list[int] = []
    for name in ("b2b-1", "b2b-2", "b2b-3"):
        wid = launch_watcher(
            "import ava\n",
            "1h",
            name,
            send=_ignore_send,
            create_session=_fake_create,
            live_session_ids=lambda: set(alive),
        )
        ids.append(wid)

    for wid in ids:
        assert (d / f"watcher_{wid}.py").exists(), f"watcher_{wid}.py pruned"
        assert (d / f"watcher_{wid}_boot.py").exists(), f"watcher_{wid}_boot.py pruned"
    assert len(ids) == 3 and len(set(ids)) == 3


def test_prune_preserves_all_files_when_session_listing_fails(_agent_row: int) -> None:
    d = _watchers_dir()
    paths = [d / "watcher_42.py", d / "watcher_42_boot.py", d / "watcher_42_launch.sh"]
    for path in paths:
        path.write_text("still needed")

    files.prune_stale(d / "watcher_43.py", None)
    assert all(path.read_text() == "still needed" for path in paths)
