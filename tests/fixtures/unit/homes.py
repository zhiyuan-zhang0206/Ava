"""Isolated home and workspace fixtures, declared beside their consumers."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.pin_agent import pin_agent


@pytest.fixture
def unit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point this unit's home at a fresh per-test tmp dir and reset machine
    identity so a test that writes machine_name/role/description files (or any
    $AVA_HOME-derived path) is isolated from the session home.

    The dir it yields is BARE — no `machine_name`, no `role`, no `.env`. That is
    not an oversight to fix: this fixture legitimately models two different
    homes, and an empty dir is the only state both of them start from.

    * A **virgin home** — never installed, never through `ava start`. Tests here
      assert the ABSENCE of the identity files, e.g.
      `test_start_arg_writes_to_file_for_persistence`, whose premise is "all env
      empty, files also do not exist".
    * An **installed unit** — a home some earlier step gave an identity. The CLI
      tests that run `ava skill install` need this one: a local-path install
      records `local:<machine>` provenance in the cluster registry, and
      `machine_name()` raises when the file is missing.

    **Do not pre-write an identity here to make the second kind pass.** It looks
    free — the installed-unit tests go green and nothing else obviously cares —
    but it silently converts every `unit_home` in the repo to the installed
    meaning. The only tests that can object are the few whose whole premise is
    the absence, and they object only if their premise was written down; the
    rest stay green while quietly testing a different world.

    The opt-in for the installed meaning is `_installed_machine_identity` in
    `tests/path_scoped/cli_tests.py` — a module takes it with one line:
    `pytestmark = pytest.mark.usefixtures("_installed_machine_identity")`.
    """
    from base.cluster.machine import reset_identity

    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    reset_identity()
    yield tmp_path
    reset_identity()


@pytest.fixture
def default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake OS user whose `~/.ava` is this test's home, and so the default home.

    OS jobs belong to the default home only (`base.host.system.cron.owns_os_jobs`),
    so a test of a registrar or unregistrar needs this, not `unit_home`. `HOME`
    points into the tmp dir, which also keeps `~/Library/LaunchAgents` and the
    crontab lock off the operator's real ones; yields the user's home, whose
    `.ava` is the default home."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("AVA_HOME", str(user_home / ".ava"))
    return user_home


@pytest.fixture
def workspace(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's agent workspace dir (the relative-path base of ava.files /
    ava.shell.run / ava.understand path mode), under the per-test unit home.

    Pins the agent id explicitly instead of relying on the session default context
    (`pin_agent(1)` in env_bootstrap) staying unmutated across test
    ordering (a leak through it is exactly what the `_isolated_agent`
    fix in tests/path_scoped/ava_tests.py guards against). The dir is NOT
    pre-created — `workspace_dir` mkdirs on first resolution, and several
    tests assert exactly that; pre-create with `.mkdir(parents=True)` when a
    test seeds files into it.
    """
    pin_agent(1)
    return unit_home / "workspaces" / "1"
