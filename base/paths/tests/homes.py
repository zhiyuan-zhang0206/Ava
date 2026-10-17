"""Isolated bare and default-home fixtures, declared beside their consumers."""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def unit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point this unit's home at a fresh per-test tmp dir. Machine caches belong
    to explicit consumer roots; this fixture only owns the home environment.

    The dir it yields is BARE — no `machine_name`, no `role`, no `.env`. That is
    not an oversight to fix: this fixture legitimately models two different
    homes, and an empty dir is the only state both of them start from.

    * A **virgin home** — never installed, never through `meiva start`. Tests here
      assert the ABSENCE of the identity files, e.g.
      `test_start_arg_writes_to_file_for_persistence`, whose premise is "all env
      empty, files also do not exist".
    * An **installed unit** — a home some earlier step gave an identity. The CLI
      tests that run `meiva skill install` need this one: a local-path install
      records `local:<machine>` provenance in the cluster registry, and
      its explicit machine owner rejects a missing name.

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
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake OS user whose `~/.meiva` is this test's home, and so the default home.

    OS jobs belong to the default home only (`base.host.system.cron.owns_os_jobs`),
    so a test of a registrar or unregistrar needs this, not `unit_home`. `HOME`
    points into the tmp dir, which also keeps `~/Library/LaunchAgents` and the
    crontab lock off the operator's real ones; yields the user's home, whose
    `.meiva` is the default home."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("MEIVA_HOME", str(user_home / ".meiva"))
    return user_home
