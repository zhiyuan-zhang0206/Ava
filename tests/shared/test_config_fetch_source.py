"""Settings-build config-source behavior, exercised in subprocesses.

The config source is role-derived (AVA_CONFIG_SOURCE deleted 2026-08-01):

- gateway-capable unit (`AVA_MACHINE_SERVE_GATEWAY=true`) -> the local .env is
  the source; no fetch.
- pure agent-runner + no AVA_CONFIG_FETCH=skip -> the Settings import fetches
  `GET /api/bootstrap` from AVA_GATEWAY_URL and the fetched values are
  authoritative.
- pure agent-runner + AVA_CONFIG_FETCH=skip (maintenance verbs, settings-lite)
  -> no fetch; the required data-plane URLs fall back to never-dialed
  placeholders so Settings constructs with the gateway down.

These run in subprocesses because `shared.config` builds its Settings singleton
at import, once per process — the pytest process already built it from the
suite's own env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from shared.dotenv_boot import UNANCHORED_DB_SENTINEL

_LITE_REDIS_URL = "redis://config-lite@127.0.0.1:1/0"


def _base_env(home: Path) -> dict[str, str]:
    """The suite env minus the pins this test must control."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AVA_CONFIG_FETCH",
            "AVA_MACHINE_SERVE_GATEWAY",
            "AVA_GATEWAY_URL",
            "AVA_DB_URL",
            "AVA_REDIS_URL",
            "AVA_CLUSTER_SECRET",
        }
    }
    env["AVA_HOME"] = str(home)
    env["AVA_HOME_OVERRIDE"] = "1"
    return env


def _import_settings(
    env: dict[str, str], code: str = "import shared.config"
) -> subprocess.CompletedProcess[str]:
    """Run `python -c <code>` against this checkout with `env`."""
    return subprocess.run(  # noqa: S603 — fixed argv, repo code, no shell
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],  # repo root
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_lite_settings_build_plants_placeholders(tmp_path: Path) -> None:
    """Maintenance-verb mode: Settings constructs with the gateway down, the
    data-plane URLs being never-dialed placeholders (lite verbs never touch the
    data plane)."""
    env = _base_env(tmp_path)
    env["AVA_CONFIG_FETCH"] = "skip"
    env["AVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    result = _import_settings(
        env,
        "import shared.config as c; print(c.settings.data_plane.db_url); "
        "print(c.settings.data_plane.redis_url)",
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == UNANCHORED_DB_SENTINEL
    assert lines[1] == _LITE_REDIS_URL


def test_unenrolled_runner_constructs_locally(tmp_path: Path) -> None:
    """A not-yet-enrolled runner (agent-runner flag on, no gateway URL) must not
    blow up the Settings import — lint scripts and tools import shared.config on
    any machine. The fail-fast for an unenrolled `ava start` is the preflight
    gate (AVA_GATEWAY_URL check), not the import."""
    env = _base_env(tmp_path)
    env["AVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    result = _import_settings(
        env,
        "import shared.config as c; print(c.settings.data_plane.db_url)",
    )
    assert result.returncode == 0, result.stderr
    assert UNANCHORED_DB_SENTINEL in result.stdout


def test_bare_checkout_constructs_locally(tmp_path: Path) -> None:
    """No role flags at all (CI / lint / dev tools): local env/.env, no fetch,
    no error."""
    env = _base_env(tmp_path)
    result = _import_settings(
        env,
        "import shared.config as c; print(c.settings.data_plane.db_url)",
    )
    assert result.returncode == 0, result.stderr
    assert UNANCHORED_DB_SENTINEL in result.stdout


def test_leaked_machine_identity_flags_do_not_flip_config_source(
    tmp_path: Path,
) -> None:
    """Regression for #771: a host shell leaking prod's machine-identity env
    (AVA_MACHINE_SERVE_GATEWAY=true, the runner flag, prod's data-plane URLs)
    into a child process must not flip an isolated unit into 'gateway-capable'.

    Before the fix the leaked flag made `config_source_is_local()` True, so the
    settings-lite placeholders were skipped; the DERIVED drop then removed the
    inherited AVA_DB_URL / AVA_REDIS_URL (the empty home's .env declares
    nothing), and Settings failed with Field required — the local-only watcher
    test failures. Identity keys a unit's own .env does not declare are now
    dropped at load_ava_env, so the child falls through to its own (absent)
    files, stays a bare checkout, and Settings constructs with the never-dialed
    placeholders."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AVA_CONFIG_FETCH",
            "AVA_GATEWAY_URL",
            "AVA_DB_URL",
            "AVA_REDIS_URL",
            "AVA_CLUSTER_SECRET",
        }
    }
    env["AVA_HOME"] = str(tmp_path)
    env["AVA_HOME_OVERRIDE"] = "1"
    # The leak: prod's identity + data-plane values riding in via the shell.
    env["AVA_MACHINE_SERVE_GATEWAY"] = "true"
    env["AVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    env["AVA_DB_URL"] = "postgresql://prod@db:5432/ava"
    env["AVA_REDIS_URL"] = "redis://prod:6380/0"
    result = _import_settings(
        env,
        "import shared.config as c; print(c.settings.data_plane.db_url)",
    )
    assert result.returncode == 0, result.stderr
    assert UNANCHORED_DB_SENTINEL in result.stdout


def test_enrolled_runner_with_unreachable_gateway_fails_fast(tmp_path: Path) -> None:
    """The configured-runner fail-fast: enrolled (flag + URL) but the gateway is
    unreachable — the import raises BootstrapFetchError naming the remedy.

    The runner flag is DECLARED IN THE UNIT'S .env, as `ava enroll` writes it —
    a machine-identity key supplied by env alone is now dropped at
    load_ava_env (#771), so an env-only flag no longer configures the fetch."""
    home = tmp_path / "home"
    home.mkdir()
    # enroll's bootstrap env: the role flag + gateway URL + cluster secret.
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_GATEWAY_URL=http://127.0.0.1:1\n"  # nothing listens on port 1
        "AVA_CLUSTER_SECRET=test-cluster-secret\n"
    )
    env = _base_env(home)
    env["AVA_GATEWAY_URL"] = "http://127.0.0.1:1"
    env["AVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — the suite's fixture value
    result = _import_settings(env)
    assert result.returncode != 0
    assert "127.0.0.1:1" in result.stderr


def test_gateway_unit_never_fetches(tmp_path: Path) -> None:
    """A gateway-capable unit builds Settings from its own .env — no fetch, no
    gateway URL needed (a local unit is its own source)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_DB_URL=postgresql://ava:s@127.0.0.1:5433/ava\n"
        "AVA_REDIS_URL=redis://127.0.0.1:6380/0\n"
    )
    env = _base_env(home)
    env["AVA_MACHINE_SERVE_GATEWAY"] = "true"
    result = _import_settings(
        env,
        "import shared.config as c; print(c.settings.data_plane.db_url)",
    )
    assert result.returncode == 0, result.stderr
    assert "postgresql://ava:s@127.0.0.1:5433/ava" in result.stdout


class _BootstrapHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, str]] = {}
    # Query strings of every request — the runner fetch must ask for the
    # least-privilege projection (Task #1236). A list: `self.path = ...` would
    # shadow a ClassVar with an instance attribute, so record into a mutable
    # class-level container instead.
    queries: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path != "/api/bootstrap":
            self.send_response(404)
            self.end_headers()
            return
        _BootstrapHandler.queries.append(query)
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_pure_runner_settings_build_fetches_and_overrides(tmp_path: Path) -> None:
    """The money path: a pure runner's Settings import fetches /api/bootstrap
    and the fetched values are authoritative — including over a stale
    pre-cutover .env materialization (migration tolerance)."""
    handler = _BootstrapHandler
    handler.payload = {
        "AVA_DB_URL": "postgresql://ava:fetched@db:5432/ava",
        "AVA_REDIS_URL": "redis://db:6380/0",
        "DEEPSEEK_API_KEY": "sk-fetched",
        "AVA_EVENTS_CHANNEL": "ava:events",
    }
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        home = tmp_path / "home"
        home.mkdir()
        # A pre-cutover enroll left stale cluster facts in the .env — tolerated:
        # the fetch must override them. (AVA_GATEWAY_URL is in derived_env_keys(),
        # so `_enforce_cluster_env_authority` forces the file value over an
        # inherited env — write the real port into the file.)
        (home / ".env").write_text(
            "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            f"AVA_GATEWAY_URL=http://127.0.0.1:{port}\n"
            "AVA_DB_URL=postgresql://stale@old:5432/ava\n"
        )
        env = _base_env(home)
        env["AVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
        env["AVA_GATEWAY_URL"] = f"http://127.0.0.1:{port}"
        env["AVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — the suite's fixture value
        result = _import_settings(
            env,
            "import shared.config as c; print(c.settings.data_plane.db_url); "
            "print(c.settings.data_plane.events_channel)",
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        # The fetched URL wins over the stale .env value; DataPlaneSettings then
        # re-applies the cluster secret as the URL password (auth is always on).
        assert lines[0] == "postgresql://ava:test-cluster-secret@db:5432/ava", lines
        assert lines[1] == "ava:events", lines
        # The runner's Settings-build fetch requests the runner projection.
        assert handler.queries == ["role=runner"], handler.queries
    finally:
        server.shutdown()
        server.server_close()


def test_runner_daemon_boot_from_session_env_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon-boot chain under the 2026-08-02 session-env policy, end to end:

    a watchdog/start process on a pure runner forwards its env through
    `shared.session_env`'s session-env handoff (`forward_env_dict` for `ava start`
    children and the watchdog respawn) — host-scope keys only, cluster-scope
    keys (a stale frozen copy) dropped. The daemon then
    boots from that env: load_ava_env, then the Settings-build fetch from the
    gateway, whose values must be the ones it actually runs with.

    Simulated: spawner env carries a STALE cluster copy (db URL on an old
    address, an old provider key); the handoff must not carry it; the child
    must fetch fresh values from the gateway instead.
    """

    from shared import session_env

    handler = _BootstrapHandler
    handler.payload = {
        "AVA_DB_URL": "postgresql://ava:fetched@db:5432/ava",
        "AVA_REDIS_URL": "redis://db:6380/0",
        "DEEPSEEK_API_KEY": "sk-fetched",
        "AVA_EVENTS_CHANNEL": "ava:events",
    }
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        home = tmp_path / "home"
        home.mkdir()
        # enroll's bootstrap env: gateway URL + cluster secret + the role flag
        # (the ONLY cluster facts a runner's .env holds since 2026-08-01).
        (home / ".env").write_text(
            "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            f"AVA_GATEWAY_URL=http://127.0.0.1:{port}\n"
            "AVA_CLUSTER_SECRET=test-cluster-secret\n"
        )
        spawner_env = {
            # host-scope facts the spawner's own boot loaded — must ride through
            "AVA_HOME": str(home),
            "AVA_MACHINE_HOST": "10.0.0.9",
            "AVA_GATEWAY_URL": f"http://127.0.0.1:{port}",
            # a stale frozen cluster copy (rotated since the spawner froze it)
            # — must NOT ride through: the child re-fetches
            "AVA_CLUSTER_SECRET": "test-cluster-secret",
            "AVA_DB_URL": "postgresql://stale@old:5432/ava",
            "DEEPSEEK_API_KEY": "sk-stale",
            "AVA_EVENTS_CHANNEL": "ava:stale-events",
            "PATH": "/usr/bin:/bin",
        }
        monkeypatch.setattr(os, "environ", dict(spawner_env))

        # `ava start`'s child env: host-scope kept, cluster-scope gone.
        child = session_env.forward_env_dict()
        assert child["AVA_HOME"] == str(home)
        assert child["AVA_MACHINE_HOST"] == "10.0.0.9"
        assert child["AVA_GATEWAY_URL"] == f"http://127.0.0.1:{port}"
        assert "AVA_DB_URL" not in child
        assert "AVA_CLUSTER_SECRET" not in child
        assert "DEEPSEEK_API_KEY" not in child
        assert "AVA_EVENTS_CHANNEL" not in child

        # The watchdog respawn path: the child's REAL environment (the backend
        # hands the dict out-of-band; no shell prefix, no handoff file).
        child_env = dict(child)
        child_env.update({"HOME": str(tmp_path)})
        code = (
            "import os; import shared.config as c; "
            "print(c.settings.data_plane.db_url); "
            "print(os.environ['DEEPSEEK_API_KEY']); "
            "print(os.environ['AVA_HOME'])"
        )
        result = subprocess.run(  # noqa: S603 — fixed argv, repo code, no shell interpolation
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],  # repo root
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        # the FETCHED config is what the daemon runs with — not the stale copy
        # the spawner froze (the URL's password is the cluster secret re-applied
        # by DataPlaneSettings: auth is always on)
        assert lines[0] == "postgresql://ava:test-cluster-secret@db:5432/ava", lines
        assert lines[1] == "sk-fetched", lines
        # host-scope facts survived the handoff
        assert lines[2] == str(home), lines
    finally:
        server.shutdown()
        server.server_close()
