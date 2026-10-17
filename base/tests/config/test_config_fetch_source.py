"""Settings-build config-source behavior, exercised in subprocesses.

The config source is role-derived (MEIVA_CONFIG_SOURCE deleted 2026-08-01):

- gateway-capable unit (`MEIVA_MACHINE_SERVE_GATEWAY=true`) -> the local .env is
  the source; no fetch.
- pure agent-runner + no MEIVA_CONFIG_FETCH=skip -> the Settings import fetches
  `GET /api/bootstrap` from MEIVA_GATEWAY_URL and the fetched values are
  authoritative.
- pure agent-runner + MEIVA_CONFIG_FETCH=skip (maintenance verbs, settings-lite)
  -> no fetch; the required data-plane URLs fall back to never-dialed
  placeholders so Settings constructs with the gateway down.

These run in subprocesses because `base.config` builds its Settings singleton
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

from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

_LITE_REDIS_URL = "redis://config-lite@127.0.0.1:1/0"
_CHILD = Path(__file__).with_name("_config_child.py")


def _base_env(home: Path) -> dict[str, str]:
    """The suite env minus the pins this test must control."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "MEIVA_CONFIG_FETCH",
            "MEIVA_MACHINE_SERVE_GATEWAY",
            "MEIVA_GATEWAY_URL",
            "MEIVA_DB_URL",
            "MEIVA_REDIS_URL",
            "MEIVA_CLUSTER_SECRET",
        }
    }
    env["MEIVA_HOME"] = str(home)
    return env


def _import_settings(env: dict[str, str], mode: str = "import") -> subprocess.CompletedProcess[str]:
    """Run the settings-import child script in `mode` against this checkout with `env`."""
    return subprocess.run(  # noqa: S603 — fixed argv, repo code, no shell
        [sys.executable, str(_CHILD), mode],
        cwd=Path(__file__).resolve().parents[3],  # repo root
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
    env["MEIVA_CONFIG_FETCH"] = "skip"
    env["MEIVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    result = _import_settings(env, "data_plane_urls")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == PLACEHOLDER_DB_URL
    assert lines[1] == _LITE_REDIS_URL


def test_unenrolled_runner_constructs_locally(tmp_path: Path) -> None:
    """A not-yet-enrolled runner (agent-runner flag on, no gateway URL) must not
    blow up the Settings import — lint scripts and tools import base.config on
    any machine. The fail-fast for an unenrolled `meiva start` is the preflight
    gate (MEIVA_GATEWAY_URL check), not the import."""
    env = _base_env(tmp_path)
    env["MEIVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    result = _import_settings(env, "db_url")
    assert result.returncode == 0, result.stderr
    assert PLACEHOLDER_DB_URL in result.stdout


def test_bare_checkout_constructs_locally(tmp_path: Path) -> None:
    """No role flags at all (CI / lint / dev tools): local env/.env, no fetch,
    no error."""
    env = _base_env(tmp_path)
    result = _import_settings(env, "db_url")
    assert result.returncode == 0, result.stderr
    assert PLACEHOLDER_DB_URL in result.stdout


def test_leaked_machine_identity_flags_do_not_flip_config_source(
    tmp_path: Path,
) -> None:
    """Regression for #771: a host shell leaking prod's machine-identity env
    (MEIVA_MACHINE_SERVE_GATEWAY=true, the runner flag, prod's data-plane URLs)
    into a child process must not flip an isolated unit into 'gateway-capable'.

    Before the fix the leaked flag made `config_source_is_local()` True, so the
    settings-lite placeholders were skipped; the DERIVED drop then removed the
    inherited MEIVA_DB_URL / MEIVA_REDIS_URL (the empty home's .env declares
    nothing), and Settings failed with Field required — the local-only watcher
    test failures. Identity keys a unit's own .env does not declare are now
    dropped at load_meiva_env, so the child falls through to its own (absent)
    files, stays a bare checkout, and Settings constructs with the never-dialed
    placeholders."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "MEIVA_CONFIG_FETCH",
            "MEIVA_GATEWAY_URL",
            "MEIVA_DB_URL",
            "MEIVA_REDIS_URL",
            "MEIVA_CLUSTER_SECRET",
        }
    }
    env["MEIVA_HOME"] = str(tmp_path)
    # The leak: prod's identity + data-plane values riding in via the shell.
    env["MEIVA_MACHINE_SERVE_GATEWAY"] = "true"
    env["MEIVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
    env["MEIVA_DB_URL"] = "postgresql://prod@db:5432/meiva"
    env["MEIVA_REDIS_URL"] = "redis://prod:6380/0"
    result = _import_settings(env, "db_url")
    assert result.returncode == 0, result.stderr
    assert PLACEHOLDER_DB_URL in result.stdout


def test_enrolled_runner_with_unreachable_gateway_fails_fast(tmp_path: Path) -> None:
    """The configured-runner fail-fast: enrolled (flag + URL) but the gateway is
    unreachable — the import raises BootstrapFetchError naming the remedy.

    The runner flag is DECLARED IN THE UNIT'S .env, as its first start writes it —
    a machine-identity key supplied by env alone is now dropped at
    load_meiva_env (#771), so an env-only flag no longer configures the fetch."""
    home = tmp_path / "home"
    home.mkdir()
    # A runner's bootstrap env: the role flag + gateway URL + a bearer.
    (home / ".env").write_text(
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "MEIVA_GATEWAY_URL=http://127.0.0.1:1\n"  # nothing listens on port 1
        "MEIVA_CLUSTER_SECRET=test-cluster-secret\n"
    )
    env = _base_env(home)
    env["MEIVA_GATEWAY_URL"] = "http://127.0.0.1:1"
    env["MEIVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — the suite's fixture value
    result = _import_settings(env)
    assert result.returncode != 0
    assert "127.0.0.1:1" in result.stderr


def test_gateway_unit_never_fetches(tmp_path: Path) -> None:
    """A gateway-capable unit builds Settings from its own .env — no fetch, no
    gateway URL needed (a local unit is its own source)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "MEIVA_DB_URL=postgresql://meiva:s@127.0.0.1:5433/meiva\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:6380/0\n"
    )
    env = _base_env(home)
    env["MEIVA_MACHINE_SERVE_GATEWAY"] = "true"
    result = _import_settings(env, "db_url")
    assert result.returncode == 0, result.stderr
    assert "postgresql://meiva:s@127.0.0.1:5433/meiva" in result.stdout


class _BootstrapHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, str]] = {}
    # Query strings of every request — the runner fetch asks for no projection:
    # bootstrap serves configuration only. A list: `self.path = ...` would
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
    cluster fact left in the runner's .env."""
    handler = _BootstrapHandler
    handler.queries.clear()
    handler.payload = {
        "MEIVA_DB_URL": "postgresql://meiva_runner@db:5432/meiva",
        "MEIVA_REDIS_URL": "redis://db:6380/0",
        "DEEPSEEK_API_KEY": "sk-fetched",
        "MEIVA_EVENTS_CHANNEL": "meiva:events",
    }
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        home = tmp_path / "home"
        home.mkdir()
        # Stale cluster facts left in the .env: the fetch must override them. (MEIVA_GATEWAY_URL is in derived_env_keys(),
        # so `_enforce_cluster_env_authority` forces the file value over an
        # inherited env — write the real port into the file.)
        (home / ".env").write_text(
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            f"MEIVA_GATEWAY_URL=http://127.0.0.1:{port}\n"
            "MEIVA_DB_URL=postgresql://stale@old:5432/meiva\n"
        )
        env = _base_env(home)
        env["MEIVA_MACHINE_SERVE_AGENT_RUNNER"] = "true"
        env["MEIVA_GATEWAY_URL"] = f"http://127.0.0.1:{port}"
        env["MEIVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — the suite's fixture value
        result = _import_settings(env, "db_url_events_channel")
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        # The fetched endpoint wins over the stale owner URL.
        assert lines[0] == "postgresql://meiva_runner@db:5432/meiva", lines
        assert lines[1] == "meiva:events", lines
        # The runner's Settings-build fetch requests no projection.
        assert handler.queries == [""], handler.queries
    finally:
        server.shutdown()
        server.server_close()


def test_runner_daemon_boot_from_session_env_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon-boot chain under the 2026-08-02 session-env policy, end to end:

    a watchdog/start process on a pure runner forwards its env through
    `base.sessions.env_forwarding`'s session-env handoff (`forward_env_dict` for `meiva start`
    children and the watchdog respawn) — host-scope keys only, cluster-scope
    keys (a stale frozen copy) dropped. The daemon then
    boots from that env: load_meiva_env, then the Settings-build fetch from the
    gateway, whose values must be the ones it actually runs with.

    Simulated: spawner env carries a STALE cluster copy (db URL on an old
    address, an old provider key); the handoff must not carry it; the child
    must fetch fresh values from the gateway instead.
    """

    from base.sessions import env_forwarding

    handler = _BootstrapHandler
    handler.payload = {
        "MEIVA_DB_URL": "postgresql://meiva@db:5432/meiva",
        "MEIVA_REDIS_URL": "redis://db:6380/0",
        "DEEPSEEK_API_KEY": "sk-fetched",
        "MEIVA_EVENTS_CHANNEL": "meiva:events",
    }
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        home = tmp_path / "home"
        home.mkdir()
        # A runner's bootstrap env: gateway URL + a bearer + the role flag
        # (the ONLY cluster facts a runner's .env holds since 2026-08-01).
        (home / ".env").write_text(
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            f"MEIVA_GATEWAY_URL=http://127.0.0.1:{port}\n"
            "MEIVA_CLUSTER_SECRET=test-cluster-secret\n"
        )
        spawner_env = {
            # host-scope facts the spawner's own boot loaded — must ride through
            "MEIVA_HOME": str(home),
            "MEIVA_MACHINE_HOST": "10.0.0.9",
            "MEIVA_GATEWAY_URL": f"http://127.0.0.1:{port}",
            # a stale frozen cluster copy (rotated since the spawner froze it)
            # — must NOT ride through: the child re-fetches
            "MEIVA_CLUSTER_SECRET": "test-cluster-secret",
            "MEIVA_DB_URL": "postgresql://stale@old:5432/meiva",
            "DEEPSEEK_API_KEY": "sk-stale",
            "MEIVA_EVENTS_CHANNEL": "meiva:stale-events",
            "PATH": "/usr/bin:/bin",
        }
        monkeypatch.setattr(os, "environ", dict(spawner_env))

        # `meiva start`'s child env: host-scope kept, cluster-scope gone.
        child = env_forwarding.forward_env_dict()
        assert child["MEIVA_HOME"] == str(home)
        assert child["MEIVA_MACHINE_HOST"] == "10.0.0.9"
        assert child["MEIVA_GATEWAY_URL"] == f"http://127.0.0.1:{port}"
        assert "MEIVA_DB_URL" not in child
        assert "MEIVA_CLUSTER_SECRET" not in child
        assert "DEEPSEEK_API_KEY" not in child
        assert "MEIVA_EVENTS_CHANNEL" not in child

        # The watchdog respawn path: the child's REAL environment (the backend
        # hands the dict out-of-band; no shell prefix, no handoff file).
        child_env = dict(child)
        child_env.update({"HOME": str(tmp_path)})
        result = subprocess.run(  # noqa: S603 — fixed argv, repo code, no shell interpolation
            [sys.executable, str(_CHILD), "respawn"],
            cwd=Path(__file__).resolve().parents[3],  # repo root
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        # The fetched endpoint is what the daemon runs with — not the stale
        # owner URL the spawner froze.
        assert lines[0] == "postgresql://meiva@db:5432/meiva", lines
        assert lines[1] == "sk-fetched", lines
        # host-scope facts survived the handoff
        assert lines[2] == str(home), lines
    finally:
        server.shutdown()
        server.server_close()
