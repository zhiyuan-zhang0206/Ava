"""Pytest fixtures.

This conftest creates an **independent `ava_test_<PID>_<TS>` database for each pytest session** at import time
— multiple concurrent sessions (multiple dev agents + CI runner + user manually running) are
isolated and cannot see each other. Also adds `<suffix>` to Redis pub/sub channel names to scatter them.

History: once shared `ava_test` single database, two pytest on the same box running simultaneously TRUNCATE each other,
stumbled upon during debugging; also hit test publishing `agent_id=1` events leaking into dev Ava
Terminal (when test channel shares with dev channel).

Startup cost: CREATE DATABASE + apply schema + saver.setup ≈ 500ms-1s. Running `pytest
test_foo.py::test_bar` single test + 1s startup, acceptable.

Orphan DB GC: scan at session import, `ava_test_<...>` older than 1h directly drop —
leftovers when pytest crashes / SIGKILL without running sessionfinish. 1h threshold far larger than business test total time.

Sync vs async fixtures:
- `db_conn`  (sync `psycopg.Connection`) — for root `db.py` shared helpers and
  UI related tests
- `adb_conn` (async `psycopg.AsyncConnection`) — for kernel `agent/db.py`
"""

# ruff: noqa: E402 — this file's project imports deliberately sit BELOW the env
# block, because those env vars are read at import time (see the block comment).
# E402's own allowlist happens to permit bare `os.environ` assignments before an
# import, but not the `mkdtemp()` call or the assertion that make the ordering
# correct and enforced. `_assert_env_precedes_project_imports()` checks the real
# invariant — that no project module was imported early — which is stricter than
# what E402 can express here.

import contextlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Generator, Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple

# Third-party only. These import no project module, so they are safe above the
# env block; every `shared.*` / `ava.*` / `tests.*` import is below it.
import psycopg
import pytest
import pytest_asyncio
import redis


@pytest.fixture(autouse=True)
def isolate_runtime_incarnation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test's process admission must not become another test's exit identity."""
    from shared import runtime_incarnation

    monkeypatch.setattr(runtime_incarnation, "_current", None)


from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver

from tests._test_env_file import rewrite_line as _rewrite_test_env_file_line

# ═══ THIS BLOCK MUST STAY ABOVE EVERY PROJECT IMPORT ═══════════════════════════
#
# Not a style preference — the env vars below are read at IMPORT time by module
# level code, so a project import above them is already too late and nothing
# says so. `_assert_env_precedes_project_imports()` at the end of the block is
# what makes that violation loud instead of invisible.
#
# ── macOS + Homebrew Postgres: the postmaster needs a real locale ──
#
# PostgreSQL's postmaster aborts with "postmaster became multithreaded during
# startup" (HINT: set LC_ALL to a valid locale) on macOS when the locale
# environment is missing: locale init goes through CoreFoundation, which
# spawns a thread, and the postmaster refuses to run multithreaded. The suite
# provisions its own throwaway postmaster via `pg_ctl` (shared/pg_tools.py),
# which inherits this process's environment, so pinning LC_ALL here — above
# every project import and any postmaster spawn — fixes every run on a box
# whose shell (session backend / launchd / CI) never set it. Only macOS needs this;
# Linux CI ships its own locale and would fail on a missing en_US.UTF-8.
if sys.platform == "darwin":
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")

# ── The unit's home: redirected here, not further down ──
#
# `shared.dotenv_boot` resolves the home ONCE at import (`resolve_ava_home()` ->
# `_HOME` -> `AVA_ENV_PATH`, module constants) and `resolve_ava_home` reads
# AVA_HOME first. So whichever runs first wins permanently: set AVA_HOME before
# that import and the whole boot is redirected; set it after and the constant is
# already the operator's real `~/.ava/.env`, with no later assignment able to
# move it.
#
# It used to be set after `import ava`, and the consequences were not academic:
#
#   - `load_ava_env()` read the operator's REAL `~/.ava/.env`, and
#     `_enforce_cluster_env_authority()` force-assigns every derived_env_keys()
#     entry (`os.environ[key] = val`, overriding what is already there). So the
#     production AVA_CLUSTER_SECRET, AVA_DB_URL and AVA_REDIS_URL landed in the
#     test process, overwriting the sentinels set a few lines earlier. A test
#     suite holding production credentials is the real severity here; the file
#     write below is the visible symptom, not the problem.
#   - (pre-2026-08-01) AVA_CONFIG_SOURCE=gateway came in with them, which was
#     the condition `shared/config/__init__.py` checked before calling
#     `inject_config_from_gateway()`. Every pytest run on an enrolled box made a
#     real authenticated GET to the production gateway at import — before any
#     fixture exists, so no fixture-based guard can reach it. Today the source
#     is role-derived and the suite pins AVA_CONFIG_FETCH=skip (above), which
#     closes the same hole.
#   - `cli.start_refresh` / `cli.enroll` bind AVA_ENV_PATH from the same module,
#     so `tests/cli` rewrote the operator's real `~/.ava/.env`. That happened
#     three times on one dev box in a single night (2026-07-28/29), during a
#     production rollout, and was non-damaging only because the gateway happened
#     to be healthy each time.
#
# The general rule, and it predicts the next one: the suite isolates everything
# addressed by a value a process reads AT USE TIME, and leaks everything fixed
# BEFORE isolation takes effect. Two ways to be fixed-before: a value bound at
# import (this block's problem) and a host-global namespace with no value to
# redirect at all (the scheduler, below — same lesson, no fix available beyond
# not acting).
_SESSION_SUFFIX = f"{os.getpid()}_{int(time.time() * 1_000_000)}"
_TEST_AVA_HOME = Path(tempfile.mkdtemp(prefix=f"ava_test_home_{_SESSION_SUFFIX}_"))
os.environ["AVA_HOME"] = str(_TEST_AVA_HOME)

# No Settings construction in the suite may fetch from a gateway. The config
# source is role-derived (AVA_CONFIG_SOURCE is gone; a unit that does not serve
# the gateway fetches at Settings build), and this test process is
# agent-runner-only by default — without the skip, `import ava` below would make
# a live GET /api/bootstrap against whatever AVA_GATEWAY_URL leaks in. The
# suite's cluster-scoped values (db/redis URLs, secret) are pinned explicitly in
# this env block; subprocess tests that spawn real daemons/agents re-derive or
# re-pin their own env per test.
os.environ["AVA_CONFIG_FETCH"] = "skip"

# The redirect above is exactly the shape `resolve_ava_home` refuses: AVA_HOME
# naming one home while the executing checkout's `.ava_home` claims another. It
# refuses because a fleet agent hitting that shape migrated prod (#1059) — but
# here it is the whole point, so the suite says so explicitly. Without this, the
# suite would import-error on any worktree that ran `install.sh --worktree` (i.e.
# every worktree with its own dev cluster) while passing on a fresh CI clone,
# which has no pointer. Exported, not set on a fixture, so the subprocesses tests
# spawn from this checkout (`ava` CLI, e2e gateway) inherit the same permission.
os.environ["AVA_HOME_OVERRIDE"] = "1"

# ── Process-profile marker: the suite is profile-less, like CI ──
#
# `Settings()` reads AVA_PROCESS_PROFILE at construction to build only the
# domains that process kind consumes. The pytest process has no process kind,
# but a local run launched from a fleet agent / runner process INHERITS the
# launcher's marker (AVA_PROCESS_PROFILE=agent), so `settings` builds only the
# agent domains and gateway-domain reads fail — `settings.feishu` /
# `settings.telegram` in the im_bridge adapter tests raise "does not construct
# the 'feishu' config domain" — while CI (no marker, every domain constructed)
# stays green. Pop the marker here, before the first Settings() construction,
# so local runs match CI (2026-08-06, PR #1650 verification).
os.environ.pop("AVA_PROCESS_PROFILE", None)

# ── OS-scheduled jobs: the suite never arms one ──
#
# launchd reads ONE ~/Library/LaunchAgents per OS user; `crontab` edits ONE table
# per user; schtasks owns ONE \Ava\ folder per user. Unlike AVA_HOME / the DB /
# the ports / the session namespace, that namespace is not addressed by a value a
# process reads, so no redirect isolates it — a test-scoped $AVA_HOME still lands
# its jobs in the operator's real scheduler.
#
# Set in the environment (not only on the settings singleton) because the leak
# that motivated this was in a SUBPROCESS: the e2e gateway runs the real
# lifespan, which registers the health probe, and the pytest-process
# monkeypatch that was meant to stop it never reached the child. Nine
# `com.ava.ava_e2e_home_*.health-probe` LaunchAgents survived on a dev box,
# firing `--auto-rollback` every 300s against whatever `ava` PATH resolved to.
# `shared.os_cron.os_jobs_enabled` gates all four registrars on this; the
# unregister paths stay live so cleanup still works.
os.environ["AVA_OS_JOBS_ENABLED"] = "false"

# Import-time sentinel for the required-no-default Settings fields (db_url /
# redis_url). Set BEFORE shared.config is imported so Settings() constructs from
# the sentinel even with no AVA_DB_URL in the environment (CI / a fresh clone),
# and a stray connection that bypasses a provisioning fixture fails loudly
# instead of hitting a real database. load_dotenv(override=False) won't clobber
# these. The real per-session URLs are injected by the `_provisioned_db` /
# `_provisioned_redis` fixtures, which start throwaway native pg/redis (tests/_containers.py).
os.environ["AVA_DB_URL"] = "postgresql://unprovisioned@127.0.0.1:1/unprovisioned"
os.environ["AVA_REDIS_URL"] = "redis://127.0.0.1:1/0"

# The cluster secret is always required (it authenticates bootstrap / /ops and is
# the pg/redis password), so the suite must carry one — a URL-safe token that
# passes the cluster_secret validator. Individual auth tests monkeypatch it (incl.
# to "" for the unset-fail-closed paths).
os.environ["AVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — test fixture
# The suite's secret-bearing servers include the e2e ops daemon, which binds
# non-loopback. The deployment precondition requires a declared mode; overlay
# records the suite's private-network posture.
os.environ["AVA_TRANSPORT_ENCRYPTION"] = "overlay"

# The suite's data-plane identity is `ava_citest`, carried entirely by the URLs
# the provisioning fixtures write (names-as-data — Settings keeps username/db
# verbatim and re-applies only the password). The throwaway pg/redis provide the
# `ava_citest` role/db/ACL user (tests/_containers.py), while the prod-db guard
# (tests/ava/conftest.py, which refuses `ava`/`ava_main`) still fires if a test
# ever points at the real production database.

# PgBouncer defaults ON in prod, but the suite pins it OFF for determinism: with
# no running pooler, AVA_DB_URL (the one dial URL) stays direct, and pinning the
# toggle keeps the real ensure_cluster_instance bring-up
# (tests/integration/test_cluster_instance.py) from spawning a pgbouncer process.
# Tests that exercise the pooled path opt in explicitly (monkeypatch enabled=True).
os.environ["AVA_PGBOUNCER_ENABLED"] = "false"

# OTLP export OFF for the whole test session (AVA_TELEMETRY_OTLP_ENABLED,
# default ON since the 2026-08-11 stack decision): the pytest-process
# monkeypatch (`_otlp_export_off`) only covers the pytest process itself —
# subprocess tests (spawned gateways / agents / restarters) inherit the
# environment and were firing real OTLP/HTTP at 127.0.0.1:4318 (the production
# collector on the CI runner's host), polluting prod Loki/Prometheus with CI
# test agents (task #1201: distinct agents 1d 76 -> 131). Set in the
# ENVIRONMENT (not only on the settings singleton) exactly because the leak
# was in subprocesses. The OTLP-specific tests
# (tests/shared/test_telemetry_otlp.py) re-enable the flag and install
# in-memory providers where the path is under test.
os.environ["AVA_TELEMETRY_OTLP_ENABLED"] = "false"

# ── Host-scope identity and telemetry URLs: loopback, like CI ──
#
# The native LGTM installer renders Tempo targets from the host-scope
# telemetry settings (`settings.observability.telemetry_tempo_query_url` /
# `telemetry_tempo_endpoint`), and `_self_machine_host()` prefers the
# AVA_MACHINE_HOST env var over the home's `machine_host` file. The login
# shell exports the operator's real ~/.ava/.env into every child process (the
# 2026-08-04 shell-env leak class), and host-scope keys survive the
# cluster-scope drop in `_enforce_cluster_env_authority` — so on a dev box a
# Tailscale host override reached the suite and
# `test_ensure_renders_configs_with_native_paths_and_loopback` rendered
# `http://100.x.y.z:3200` instead of loopback while CI (no .env) stayed green.
# Pinned unconditionally, like the gateway/telegram sentinels: local runs
# resolve the same loopback host everywhere, and a test that needs a remote
# Tempo URL or host address monkeypatches the settings explicitly
# (tests/shared/test_machine.py does for machine_host).
os.environ["AVA_TELEMETRY_TEMPO_QUERY_URL"] = "http://127.0.0.1:3200"
os.environ["AVA_TELEMETRY_TEMPO_ENDPOINT"] = "http://127.0.0.1:14318"
# The OTLP ingress port is rendered into the collector config, the roster gate
# and the healthcheck probe ports; an ambient AVA_TELEMETRY_OTLP_PORT from the
# operator's .env would move every rendered endpoint off the pinned 4318 the
# render tests assert. Pinned for the same leak class as the Tempo URLs.
os.environ["AVA_TELEMETRY_OTLP_PORT"] = "4318"
os.environ["AVA_MACHINE_HOST"] = "localhost"

# ── Grafana admin credential: the suite never carries a live one ──
#
# Same leak class: the login shell exports the real GRAFANA_ADMIN_PASSWORD,
# and the native LGTM render writes `settings.alerts.grafana_admin_password`
# into the rendered native dir — a production credential flowing through test
# output. Pinned empty like the telegram token; a test that needs a credential
# monkeypatches `settings.alerts.grafana_admin_password`
# (tests/cli/test_converge_lgtm.py does).
os.environ["GRAFANA_ADMIN_PASSWORD"] = ""

# The gateway address every client-side caller resolves (`gateway_api_base()` ->
# `_resolve_gateway_url()` -> `settings.gateway.gateway_url`, whose field default
# is ""). It is read off the Settings singleton, so it has to be in the
# environment before `import ava` builds that singleton — a later assignment
# cannot reach it.
#
# This is the one value the suite was getting purely by accident. It used to be a
# `setdefault` further down, which was dead code twice over: the operator's real
# ~/.ava/.env had already supplied their live gateway URL, and even had it not,
# the assignment landed after Settings was built. Five tests (the SDK client's
# bearer/base-url pair, cmd_restart, the agent-runner self-update) passed only
# because that leaked value was there — on a host with no Ava install they raise
# `GatewayApiBaseMissing`. Pinned unconditionally, like the db/redis/secret
# sentinels above, so the suite resolves the same fake gateway everywhere instead
# of inheriting whatever the box happens to be enrolled with.
os.environ["AVA_GATEWAY_URL"] = "http://test-gateway.invalid:8000"
# ── Telegram: the suite never carries a live bot token ──
#
# On dev boxes the login shell exports the operator's real ~/.ava/.env into
# every child process (the 2026-08-04 shell-env leak class), so
# AVA_TELEGRAM_BOT_TOKEN / AVA_TELEGRAM_OWNER_ID are live here even though
# AVA_HOME is redirected: pydantic-settings precedence is env > default, and
# `_enforce_cluster_env_authority` drops only the DERIVED data-plane keys, not
# these. Anything reading `settings.telegram` then holds a working send path
# to the operator's bot. The health probe did exactly that pre-W16 — its
# owner alerts POSTed straight to the Telegram Bot API, so a unit test
# exercising a probe failure path without stubbing the alert seam sent the
# operator real "[test_...] [health-probe] cluster unhealthy" messages, four
# per local pytest run (Task #794, 2026-08-05). W16 moved the probe to
# the alerts ingest + im_bridge /send, but the leak class is general (the
# telegram skill, IM adapters, any future direct caller). Pinned empty like
# the db/redis sentinels: a test that needs a token monkeypatches
# `settings.telegram` explicitly (tests/cli/test_cluster_health.py does).
os.environ["AVA_TELEGRAM_BOT_TOKEN"] = ""
os.environ["AVA_TELEGRAM_OWNER_ID"] = "0"
# Repository providers read the process environment while spawn validation reads
# the cluster `.env` file. Seed every default provider's inert key through both
# channels before project imports so tests can select any registered model.
_TEST_PROVIDER_KEY_ENVS = (
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GLM_API_KEY",
    "MOONSHOT_API_KEY",
    "MIMO_API_KEY",
    "DASHSCOPE_API_KEY",
)
for _provider_key_env in _TEST_PROVIDER_KEY_ENVS:
    os.environ[_provider_key_env] = "sk-test"
# ── The same DERIVED keys, declared in the test home's .env ──
#
# `_enforce_cluster_env_authority` (dotenv_boot, run at `load_ava_env`) forces a
# DERIVED key the unit's .env declares, and DROPS one it does not (2026-08-02:
# generalized from the pgbouncer-port-only rule). The suite plants its cluster
# values in the ENVIRONMENT, not in a file — which worked while the drop was
# pgbouncer-only and would now delete the planted values (and make Settings
# construction fail on the required-no-default fields). Write them into the
# test home's .env as well, so the file declares what the env carries and the
# authority pass takes the force branch. Subprocess tests that spawn the real
# CLI inherit this home, so they read the same declarations.
(_TEST_AVA_HOME / ".env").write_text(
    "\n".join(
        [
            f"AVA_DB_URL={os.environ['AVA_DB_URL']}",
            f"AVA_REDIS_URL={os.environ['AVA_REDIS_URL']}",
            f"AVA_CLUSTER_SECRET={os.environ['AVA_CLUSTER_SECRET']}",
            "AVA_RUNNER_DB_PASSWORD=test-runner-db-password",
            f"AVA_TRANSPORT_ENCRYPTION={os.environ['AVA_TRANSPORT_ENCRYPTION']}",
            f"AVA_GATEWAY_URL={os.environ['AVA_GATEWAY_URL']}",
            f"AVA_TELEGRAM_BOT_TOKEN={os.environ['AVA_TELEGRAM_BOT_TOKEN']}",
            f"AVA_TELEGRAM_OWNER_ID={os.environ['AVA_TELEGRAM_OWNER_ID']}",
            *(f"{key}={os.environ[key]}" for key in _TEST_PROVIDER_KEY_ENVS),
            f"AVA_TELEMETRY_OTLP_ENABLED={os.environ['AVA_TELEMETRY_OTLP_ENABLED']}",
            f"AVA_TELEMETRY_TEMPO_QUERY_URL={os.environ['AVA_TELEMETRY_TEMPO_QUERY_URL']}",
            f"AVA_TELEMETRY_TEMPO_ENDPOINT={os.environ['AVA_TELEMETRY_TEMPO_ENDPOINT']}",
            f"AVA_MACHINE_HOST={os.environ['AVA_MACHINE_HOST']}",
            f"GRAFANA_ADMIN_PASSWORD={os.environ['GRAFANA_ADMIN_PASSWORD']}",
            # Same force-branch reasoning: exec timeout is cluster-scoped; without a
            # declaration the drop deletes the pinned 60s and the D5 fallback reads the
            # field default (300s) — test_config bootstrap/panel tests assert equality.
            "AVA_EXEC_TIMEOUT_SECONDS=60.0",  # the pinned value below; constant, not env-read (write precedes the pin)
            # Same force-branch reasoning: runner-mode is cluster-scoped, and a
            # CI job export the test home's .env does not declare would be DROPPED
            # by the authority pass at the first load_ava_env — the process-shaped
            # lifecycle suites (backend shards, e2e shards) would silently run the
            # code default (hosted since 2026-09) as a second hosted net. Declare
            # it when the environment carries it (e2e/backend CI jobs export it);
            # local runs without the var keep the code default.
            *(
                [f"AVA_RUNNER_MODE={os.environ['AVA_RUNNER_MODE']}"]
                if os.environ.get("AVA_RUNNER_MODE")
                else []
            ),
            "",
        ]
    )
)


def _assert_env_precedes_project_imports() -> None:
    """Fail loudly if a project module was imported above the env block.

    The block's whole contract is ordering, and ordering is exactly the kind of
    thing a later edit breaks silently: move one import up, or add one to a
    module the block already imports, and every assignment above still *runs* —
    it just no longer has any effect on the constants that were bound during that
    earlier import. Nothing fails, the suite goes green, and it quietly runs
    against the operator's real home again.

    So assert the precondition instead of documenting it. `tests` itself is
    excluded (this module is being imported as `tests.conftest` right now);
    `shared` is the one that matters, since it is what binds AVA_ENV_PATH.
    """
    leaked = sorted(
        name
        for name in sys.modules
        if name.split(".")[0] in {"shared", "ava", "agent", "gateway", "cli", "ops", "services"}
    )
    if leaked:
        raise RuntimeError(
            "tests/conftest.py: a project module was imported before the env block "
            f"finished: {leaked[:5]}{'...' if len(leaked) > 5 else ''}. Everything above "
            "this line sets env vars that are read at IMPORT time — AVA_HOME decides "
            "which .env `shared.dotenv_boot` binds AVA_ENV_PATH to, permanently. An "
            "import above it silently pins the suite to the operator's real ~/.ava, "
            "production credentials included. Move the import below this assertion."
        )


_assert_env_precedes_project_imports()

# ava / shared.config read AVA_DB_URL + AVA_HOME at import — must come after the
# env block above, which is what the assertion just enforced.
import ava
import shared.service_respawn
from shared.config import set_field, settings
from shared.daemon_health import _HEALTH_PORT_OVERRIDES

# The host-scope isolation pins (env block above) must have taken effect before
# Settings construction: the native LGTM render reads the Tempo URLs at use
# time, `_self_machine_host()` prefers the env var, and the render writes the
# Grafana credential into its output — the login-shell .env leak class would
# otherwise put the operator's real host address and credentials into the
# suite. Assert both the environment and the constructed settings, so a later
# edit that weakens a pin (e.g. `setdefault`) fails loudly on every box, even
# where CI cannot see the leak (a clean CI env has no ambient value to
# override, so the LGTM test alone would stay green).
assert os.environ.get("AVA_TELEMETRY_TEMPO_QUERY_URL") == "http://127.0.0.1:3200"
assert os.environ.get("AVA_TELEMETRY_TEMPO_ENDPOINT") == "http://127.0.0.1:14318"
assert os.environ.get("AVA_TELEMETRY_OTLP_PORT") == "4318"
assert os.environ.get("AVA_MACHINE_HOST") == "localhost"
assert os.environ.get("GRAFANA_ADMIN_PASSWORD") == ""
assert settings.observability.telemetry_tempo_query_url == "http://127.0.0.1:3200"
assert settings.observability.telemetry_tempo_endpoint == "http://127.0.0.1:14318"
assert settings.observability.telemetry_otlp_port == 4318
assert settings.observability.telemetry_otlp_endpoint == "http://127.0.0.1:4318"
assert settings.general.machine_host == "localhost"
assert settings.alerts.grafana_admin_password is None or (
    settings.alerts.grafana_admin_password.get_secret_value() == ""
)
from shared.platform import raise_fd_limit
from shared.test_db_guard import assert_test_db_url
from tests._containers import postgres, redis_server
from tests._os_jobs import host_ava_os_jobs, is_test_owned_job, remove_os_job

# Host job inventory as it stood BEFORE this session — `pytest_sessionfinish`
# diffs against it and fails the run on anything new (see tests/_os_jobs.py).
_OS_JOBS_AT_START = host_ava_os_jobs()

# `_SESSION_SUFFIX` (PID + microsecond suffix, zero cross-process collision) and
# the tmpfs `_TEST_AVA_HOME` it names are defined in the env block at the top of
# this file — AVA_HOME has to be in the environment before the first project
# import, so the home cannot be created down here. Reused below for Redis channel
# isolation and machine_name, so concurrent sessions (dev agents + CI + manual
# runs) never share channels or home dirs.
settings.data_plane.events_channel = f"ava:events:test:{_SESSION_SUFFIX}"
# A dev operator's .env may carry exec_timeout_seconds (≠ the field default),
# which the module-load settings reads. The timeout-marker tests
# (tests/agent/test_cancel.py) assert against the documented default, so pin it
# back — local must match CI, whose fresh .env has no such value (60s).
settings.sandbox.exec_timeout_seconds = 60.0
os.environ["AVA_EXEC_TIMEOUT_SECONDS"] = "60.0"
# Pre-warm the D5 full-instance cache while os.environ still carries the pinned
# test values. A later test may legitimately simulate the gateway profile
# (AVA_PROCESS_PROFILE=gateway + _enforce_cluster_env_authority), which POPS
# the agent-runner cluster aliases (AVA_EXEC_TIMEOUT_SECONDS among them) from
# os.environ for the rest of this worker. If the cache were first populated
# after that pop, the config-service read paths would serve the field default
# (exec_timeout_seconds=300) instead of the pinned 60 for the whole session
# (2026-08-06 CI: test_config 300-vs-60 flake). Warming it here pins the
# complete-instance snapshot at the pristine env.
from shared.config.service_read import _all_domains_settings as _prewarm_full_settings

_prewarm_full_settings()


# Session default for the SDK's own agent id. `ava.self.*` / `ava.agents.*` read
# it to address `/api/agents/<AGENT_ID>/...`; leaving it None would 404 or crash
# earlier. It is only a placeholder for tests that never create an agent: serial
# ids are no longer reset between tests (see `_clean_state` — no RESTART IDENTITY),
# so the first spawn is NOT guaranteed to be id 1. Any test that exercises
# `ava.self.*` / `ava.agents.*` re-pins this to the id it actually created via
# `ava._boot._agent_id = spawn_agent()` (the pattern used across tests/ava/*). Do
# not rely on "the first spawn is 1" — capture the returned id.
ava._boot._agent_id = 1
ava._boot._owns_loop = True
# Remove AVA_AGENT_ID propagated from the agent process — any test that
# temporarily clears _agent_id would re-establish from this env var with
# owns_loop=False, corrupting subsequent tests.
os.environ.pop("AVA_AGENT_ID", None)

# `_TEST_AVA_HOME` is created and exported as AVA_HOME in the env block at the
# top of this file. Mirror it onto the settings singleton too: the env var is what
# the import-time boot reads, this is what in-process `settings.general.ava_home`
# readers get. Both point at the same tmpfs dir, removed in `pytest_sessionfinish`.
settings.general.ava_home = _TEST_AVA_HOME
# The host-level cluster registry is independent of AVA_HOME and defaults to the
# real `~/.ava/clusters.json`; redirect it into the tmpfs home too so tests never
# read or write the operator's prod registry.
settings.general.cluster_registry = _TEST_AVA_HOME / "clusters.json"
os.environ["AVA_CLUSTER_REGISTRY"] = str(_TEST_AVA_HOME / "clusters.json")
# multi-machine setup: spawn_agent / claim_agent_row reads machine_name()
# from `$AVA_HOME/machine_name`; must write one into tmpfs first otherwise MachineNameMissing.
(_TEST_AVA_HOME / "machine_name").write_text(f"test-{_SESSION_SUFFIX}")
# machine capabilities default to agent-runner-only: gateway lifespan +
# post_agents preflight both read it, tests default to agent-runner path so that
# launch_agent_op locally launches the process; gateway-only tests monkeypatch
# override themselves.
# gateway-only tests monkeypatch override themselves. Both serve files respectively declare:
# serve_agent_runner=true, serve_gateway left empty (=off).
(_TEST_AVA_HOME / "machine_serve_agent_runner").write_text("true")
# _resolve_role reads the settings bool (env wins over the file), so pin both to
# the session default. This used to be a correction: `settings` module-loaded
# before AVA_HOME was redirected and therefore captured the host operator's
# AVA_MACHINE_SERVE_* out of their real ~/.ava/.env. With the home redirected
# before that import there is no host `.env` on the path to capture, so this is
# now belt-and-braces — kept because the default a test inherits should be stated
# here rather than left to whatever the environment happens not to contain.
settings.general.machine_serve_agent_runner = True
settings.general.machine_serve_gateway = None

# machine_role defaults to agent-runner; gateway_api_base() reads gateway_url,
# which is pinned in the env block at the top of this file (it has to precede the
# Settings construction that `import ava` triggers). Tests never open a real TCP
# connection to it — `.invalid` is reserved by RFC 2606 and cannot resolve.

# ── Service ports: the session gets its own, never a prod default ──
#
# Redirecting AVA_HOME is not enough. Every service port falls back to a FIXED
# default when nothing overrides it — restarter 8102, gateway 8000, frontend
# 3000, milvus — and those are the ports the operator's prod cluster on this same
# box is already bound to. So a test that starts a daemon binds prod's port, and
# a test that runs a healthcheck probes (or respawns!) prod's service.
#
# That is not hypothetical: on 2026-07-24 a pytest-leaked restarter daemon took
# prod's 8102 and answered its /healthz for 98 minutes, so prod's watchdog saw
# green and never revived the real restarter — the whole cluster's `restarting`
# agents froze. `pg`/`redis` were already isolated this way (tests/_containers.py
# binds :0); these were the ports that were not.
#
# Pinned in the settings singleton (in-process readers, which have already
# constructed Settings by now) AND in os.environ (any subprocess a test spawns).
# Kernel-assigned, so concurrent xdist workers — one import of this module each —
# never collide either.


def _free_port() -> int:
    """A free localhost port, released immediately. The small race before a server
    binds it is the same one tests/_containers.py accepts for pg/redis."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pin_setting(field: str, value: object) -> None:
    """Force a settings field for this session, in the singleton and in the env
    under pydantic's `AVA_<FIELD>` name (subprocesses read the latter)."""
    set_field(field, value)
    os.environ[f"AVA_{field.upper()}"] = str(value)


# Every daemon that registers a health port — read off the same map health_port()
# consults, so a newly registered daemon is isolated the moment it is added there
# rather than silently inheriting its prod default.
for _health_port_field in _HEALTH_PORT_OVERRIDES.values():
    _pin_setting(_health_port_field, _free_port())

_pin_setting("gateway_health_url", f"http://127.0.0.1:{_free_port()}/api/health")
_pin_setting("frontend_healthcheck_url", f"http://127.0.0.1:{_free_port()}")
_pin_setting("milvus_port", _free_port())

# Belt-and-suspenders on the OS-jobs switch already in `os.environ` at the top of
# this file: an operator's real `~/.ava/.env` is loaded by `shared.dotenv_boot`
# before Settings constructs, so pin the singleton rather than trust that the env
# value is what survived. (`_pin_setting` rewrites the env var too, harmlessly.)
_pin_setting("os_jobs_enabled", False)

# ── fd budget for detached session children ──
#
# The session backends (posixproc / PTY supervisor) spawn detached children from
# this process; the fd raise guarantees the launch cannot fail on a low fd limit
# (the launchd watchdog chain caps at 256, and a raised limit is load-bearing
# there — `raise_fd_limit` replaced the old runtime fd bump). Called here
# rather than left to the first lazy caller so the budget is in place before any
# test runs.
raise_fd_limit(65536)

# ── The baseline every test is entitled to see ──
#
# Snapshotted HERE, at the end of the env block and before any fixture exists, so
# it holds the environment this file deliberately built and nothing a test or a
# fixture layered on later. `tests/test_home_isolation.py` compares against it to
# catch the leak class that motivated it: a fixture whose scope outlives the
# directory it was written for reassigns a process global and restores it too late,
# so every test collected after that directory keeps running with the wrong value.
# Comparing against a captured baseline rather than against hand-written expected
# values means the check covers whatever keys the leaking fixture touches, including
# ones nobody listed.
#
# `AVA_DB_URL` / `AVA_REDIS_URL` legitimately differ from this snapshot from the
# first test onward — `_provisioned_db` / `_provisioned_redis` below replace the
# import-time sentinel with the session's real throwaway instances. That is the one
# sanctioned divergence, and it is sanctioned because those fixtures live in the
# ROOT conftest, where session scope and the blast radius are the same thing.
_PRISTINE_ENV: dict[str, str] = dict(os.environ)


@pytest.fixture(scope="session")
def pristine_env() -> Mapping[str, str]:
    """The environment as of the end of this file's env block, before any fixture ran.

    A fixture rather than an importable constant so the value travels through
    pytest's own channel — this conftest's module name depends on import mode, and a
    test that reached in by module path would be coupled to it.
    """
    return _PRISTINE_ENV


# ── Container provisioning: the fixture owns the database ──
#
# A throwaway Postgres + Redis is started once per pytest-session worker
# (autouse), and settings + env are pointed at it (replacing the import-time
# sentinel). Every test runs against a real, clean DB + Redis — `_clean_state`
# truncates tables and flushes Redis before each test. This is deliberately
# uniform rather than per-test fixture bookkeeping: most DB tests also touch
# Redis through the gateway endpoints they exercise (events publish), so gating
# Redis behind a per-test fixture was fragile (easy to miss, fails silently
# against the sentinel). The fixed cost is ~one native pg/redis pair per worker,
# started in parallel under `-n auto`.


@pytest.fixture(scope="session", autouse=True)
def _provisioned_db() -> Iterator[str]:
    with postgres() as url:
        # Belt: the throwaway provisioning must itself stay on a test database.
        # If the throwaway db name ever changes, this assertion makes the
        # change explicit (update shared/test_db_guard.py) instead of silently
        # loosening the session-start guard above.
        assert_test_db_url(url, context="_provisioned_db")
        settings.data_plane.db_url = url
        os.environ["AVA_DB_URL"] = url
        _rewrite_test_env_file_line(_TEST_AVA_HOME / ".env", "AVA_DB_URL", url)
        with PostgresSaver.from_conn_string(url) as saver:
            saver.setup()
        yield url


@pytest.fixture(scope="session", autouse=True)
def _provisioned_redis() -> Iterator[str]:
    with redis_server() as url:
        settings.data_plane.redis_url = url
        os.environ["AVA_REDIS_URL"] = url
        _rewrite_test_env_file_line(_TEST_AVA_HOME / ".env", "AVA_REDIS_URL", url)
        yield url


# Options whose value is a separate token: `pytest --ignore tests/x.py` must not
# let that value count as a positional file arg. argparse normally consumes such
# values before they reach `config.args`, but the guard must not depend on that
# (--deselect's value even looks like a file path with a `::nodeid` suffix).
_VALUE_OPTIONS = frozenset(
    {
        "--ignore",
        "--deselect",
        "--ignore-glob",
        "--rootdir",
        "--basetemp",
        "--confcutdir",
        "--cov",
        "--junitxml",
    }
)


def _full_run_guard_message(
    rootpath: Path, args: list[str], env: Mapping[str, str], cwd: Path
) -> str | None:
    if env.get("GITHUB_ACTIONS") == "true":
        return None
    if env.get("AVA_ALLOW_FULL_PYTEST") == "1":
        return None

    shared_roots = {
        (Path.home() / "Ava").resolve(),
        (Path.home() / ".ava" / "source").resolve(),
    }
    resolved_rootpath = rootpath.resolve()
    if resolved_rootpath not in shared_roots:
        return None

    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_OPTIONS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        selected_path = Path(arg.split("::", maxsplit=1)[0])
        # pytest resolves positional args against the invocation dir, not the
        # rootdir — `cd tests && pytest ava/test_x.py` is a legit targeted run
        # whose arg only exists under cwd. Fall back to the rootdir for args
        # given from the checkout root (the common case).
        if selected_path.is_absolute():
            candidates = [selected_path]
        else:
            candidates = [cwd / selected_path, resolved_rootpath / selected_path]
        if any(candidate.is_file() for candidate in candidates):
            return None

    return """\
Full local pytest runs are blocked on this shared Ava checkout.
User ruling (2026-08-24): do not run the full suite on the shared production host.
Run a targeted test instead:
  pytest tests/<path>::<func>
Directory-only selections are blocked because they still collect too much.
Full-suite verification runs in GitHub CI when you push.
For an explicitly user-approved local full run, use:
  AVA_ALLOW_FULL_PYTEST=1 pytest ...
Do not use this override without user approval."""


def pytest_configure(config: pytest.Config) -> None:
    message = _full_run_guard_message(
        config.rootpath, config.args, os.environ, config.invocation_params.dir
    )
    if message is not None:
        pytest.exit(message, returncode=1)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail fast when this pytest process resolved a non-test DB URL.

    The env block at the top of this file pins AVA_DB_URL to the unreachable
    sentinel before any project import, so on the safe path
    ``settings.data_plane.db_url`` is the sentinel here. The guard's job is
    the path where that block did NOT run: a bootstrap that stops loading this
    conftest (the 2026-08-12 incident class — a test run rooted outside this
    repo resolved the operator's real ~/.ava/.env and seeded synthetic
    agents into the production database) leaves settings pointing at whatever
    home the process resolves, and this hook refuses the whole session before
    any fixture or test runs.

    Runs in every process: each xdist worker is its own pytest session and
    re-imports this conftest, and subprocess tests inherit the pinned
    environment from their parent worker.
    """
    assert_test_db_url(settings.data_plane.db_url, context="pytest session (tests/conftest.py)")


# The per-test TRUNCATE list — single source of truth. Kept as a module
# constant so tests/test_lint_truncate_isolation.py can AST-parse it and fail
# when a new per-test data table is not covered (by this list, an FK-cascade
# from it, or an explicit exemption there). Singleton/infra tables
# (deployment_state, cluster_update_lock, ...) are deliberately absent — see
# the guard's exemption list for the reasons.
_PER_TEST_TRUNCATE_TABLES = (
    "work_failed_events",
    "inbound_messages",
    "agents_meta",
    # "events" was dropped with the task #1281/#1823 cleanup (migration
    # 20260829T030000_drop-events-archive) — the unified event stream lives in
    # Loki, and tests read it through the FakeLoki / live-stream fakes.
    "event_dismissals",
    "rollup_day_state",
    "llm_usage_hourly",
    "agents",
    "alerts",
    "machines",
    "machine_probe",
    "machine_units",
    "host_deploy_state",
    "schedules",
    "schedule_versions",
    "schedule_runs",
    "agent_presets",
    "mcp_clients",
    "api_idempotency",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    "user_settings",  # no FK, per-test key/value data (audit round-2 cc-docs-tests P2)
    "web_sessions",
    # The cluster extension registry (issue #39 S2). `extensions` FKs to
    # `extension_blobs`, so truncating the blobs cascades to the rows — but both
    # are listed, because the cascade direction is the opposite of the reading
    # order and a future row without a blob (a `source='repo'` row carries none)
    # would otherwise survive into the next test.
    "extensions",
    "extension_blobs",
)


@pytest.fixture(autouse=True)
def _restore_ava_home_override() -> Iterator[None]:
    """Restore the checkout-contradiction exemption after every test.

    The suite establishes AVA_HOME_OVERRIDE=1 before project imports, but tests
    that exercise a real stop path may consume it. Tests that monkeypatch this
    key restore the same original value ("1"), so both teardowns reassert the
    session invariant regardless of their order.
    """
    yield
    os.environ["AVA_HOME_OVERRIDE"] = "1"


@pytest.fixture(autouse=True)
def _otlp_export_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the test session hermetic: the OTLP dual-write (shared.telemetry ->
    shared.telemetry_otlp, default ON since the 2026-08-11 stack decision) would
    otherwise fire real OTLP/HTTP requests at 127.0.0.1:4318 from every
    event-emitting test. tests/shared/test_telemetry_otlp.py re-enables the
    flag and installs in-memory providers where the OTLP path is under test.
    """
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", False)


@pytest.fixture(autouse=True)
def _restore_sdk_metering() -> Iterator[None]:
    """Per-test isolation for the process-global `ava` singleton's metering state:
    whatever a test wrapped, the next test sees the bare callables again.

    `agent.graph._build._load_extensions()` calls `agent.sdk_metering.install()` as
    a side effect, which replaces every public `ava.*` callable — plus the
    `ava.mcps._call_raw` MCP funnel — with a recording proxy, and nothing ever put
    them back. `_load_extensions()` is reached directly *and* lazily, via
    `ava/__init__.py:_ensure_plugins_loaded` on an `ava.*` miss, so merely touching
    the namespace permanently swapped out the callables every later test in that
    xdist worker would see.

    That is invisible in isolation and only appears when the polluting test lands in
    the same worker — nondeterministic under `-n`, since `--dist load` distributes
    dynamically. It has already cost a night: a test asserting that `install()` wraps
    an unwrapped funnel read `install()`'s correct no-op as a failure, and the merge
    queue's bisect blamed whichever innocent PR shared the batch (issue #83, after
    #82 fixed that one test's symptom by taking its own baseline).

    Free for the tests that never metered: `uninstall()` returns immediately when
    nothing is installed, and `sys.modules` is read rather than imported so a test
    process that never pulled in the agent layer does not pull it in here.
    """
    yield
    metering = sys.modules.get("agent.sdk_metering")
    if metering is not None:
        metering.uninstall()


@pytest.fixture(autouse=True)
def _clean_state(
    _provisioned_db: str,
    _provisioned_redis: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Per-test isolation: truncate all tables and flush Redis before each test,
    so every test starts against an empty DB + Redis on the session's native pg/redis.
    Table schema is set up once per session by _provisioned_db.

    Also disables the cluster-secret auth middleware (the sanctioned
    auth_middleware_enabled=false bypass) so tests don't need to carry auth
    credentials (individual auth tests re-enable it via monkeypatch). An empty
    cluster_secret is no longer a bypass — auth is fail-closed and the gateway
    refuses to start without a secret — so the explicit flag is what disables it.
    """
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "")
    monkeypatch.setattr("shared.config.settings.gateway.auth_middleware_enabled", False)
    # Barrier before the TRUNCATE: the telemetry drain thread may hold a batch
    # dequeued during the previous test (events are written by that single
    # thread up to one flush_interval after enqueue). sync() drains the queue
    # AND waits for the held batch to land, so the events table cannot be
    # repopulated from a straggler after this truncate (the test_events_api /
    # test_agent_events_query exact-content flake class). Cheap: a marker
    # round-trip on an idle drain thread. No-op when the pipeline is absent or
    # already stopped.
    from shared import telemetry

    telemetry.sync()
    with psycopg.connect(settings.data_plane.db_url) as conn:
        # Retried on DeadlockDetected: the tolerated straggler class documented
        # on the TRUNCATE below (a background writer leaked from a prior test on
        # this worker) can also DEADLOCK this TRUNCATE, not just write to a dead
        # id. The
        # straggler's snapshot query (`shared/agent_snapshot.py`: agents_meta
        # LEFT JOIN inbound_messages) takes its relation locks in the opposite
        # order to this TRUNCATE list (inbound_messages first, agents_meta
        # second) — and other product transactions (deliver_chat_inbound) lock in
        # the TRUNCATE's order, so no single list order dodges every straggler
        # shape. Postgres aborts one side after deadlock_timeout; if it picks us,
        # the straggler has by then finished or died, so one retry wins.
        for attempt in range(3):
            try:
                with conn.cursor() as cur:
                    # TRUNCATE without RESTART IDENTITY: serial ids grow monotonically for
                    # the whole worker-session and are never reused across tests. Reuse
                    # (every test's first spawn = id 1) was the amplifier that turned a
                    # background writer leaked from a prior test into a live-row corruption
                    # of the next test's reused id (the CI-only lifecycle-state collision
                    # flake class). Monotonic ids make a straggler write land on a dead id
                    # instead. Tests that need a stable self-identity take `self_agent`.
                    cur.execute("TRUNCATE " + ", ".join(_PER_TEST_TRUNCATE_TABLES) + " CASCADE")
                conn.commit()
                break
            except psycopg.errors.DeadlockDetected:
                if attempt == 2:
                    raise
                conn.rollback()
    with redis.Redis.from_url(settings.data_plane.redis_url) as r:
        r.flushdb()
    yield


# ── Two-unit fixtures: model "a gateway unit" and "a runner unit" explicitly ──
#
# Background: this session module-loads with machine_role='agent-runner' + one
# AVA_HOME, because most SDK tests need spawn_agent to take the local-spawn path
# (the gateway invariant rejects local spawn). gateway tests have
# historically monkeypatched machine_role back per-module (e.g. patching both
# `status_router.machine_role` and `cluster_mod.machine_role`). These fixtures
# centralize the role switch at its source (the identity holder) so a test
# states which unit it exercises instead of patching every import site.
#
# They are additive: the session default (agent-runner) is unchanged, so existing
# tests keep working. New / migrated tests opt in by taking `gateway_unit` /
# `runner_unit`.


@contextlib.contextmanager
def _machine_identity(*, role: str, name: str | None = None) -> Generator[None]:
    """Switch this process's resolved machine identity, restoring on exit.

    Injects via shared.machine.set_identity so every `from shared.machine import
    machine_role` / `machine_name` call site sees the new value without
    per-module patching. `name=None` leaves machine_name as-is — no injection; it
    resolves lazily from `$AVA_HOME/machine_name` if not yet cached, otherwise
    returns the already-cached value. The finally block resets the holder, so the
    session default is restored — no per-field save/restore is needed because the
    holder re-resolves lazily after reset.
    """
    from shared.machine import reset_identity, set_identity

    if name is None:
        set_identity(role=role)  # pyright: ignore[reportArgumentType]  # str passthrough to MachineRole literal
    else:
        set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]
    try:
        yield
    finally:
        reset_identity()


@pytest.fixture
def set_machine_identity() -> Iterator[object]:
    """Factory: switch machine role (and optionally name) at the source via
    shared.machine.set_identity.

        def test_x(set_machine_identity, db_conn):
            set_machine_identity(role="gateway", name="cloud-test")

    May be called more than once within a test to flip roles; the last call
    wins, and the holder is reset at teardown (restoring the session default).
    """
    from shared.machine import reset_identity, set_identity

    def _set(role: str, name: str | None = None) -> None:
        if name is None:
            set_identity(role=role)  # pyright: ignore[reportArgumentType]
        else:
            set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]

    try:
        yield _set
    finally:
        reset_identity()


@pytest.fixture
def gateway_unit(db_conn: psycopg.Connection) -> Iterator[TestClient]:
    """The gateway unit (gateway): owns the test DB/config, runs the
    in-process gateway app, role='gateway'.

    Yields a FastAPI TestClient bound to the gateway app. Use for tests that
    exercise gateway endpoints / invariants without hand-patching
    machine_role at each import site. db_conn gives the per-test TRUNCATE. For a
    specific machine_name, also take `set_machine_identity` and call it.

    The cluster-secret auth middleware is disabled by the autouse `_clean_state`
    (auth_middleware_enabled=false) so in-process endpoint tests don't need to
    carry auth headers. Tests that specifically exercise the auth middleware
    re-enable it via monkeypatch.
    """
    from gateway.app import app as _app

    _ = db_conn  # per-test truncate side effect
    with (
        _machine_identity(role="gateway"),
        TestClient(_app, base_url="http://test-gateway") as client,
    ):
        yield client


@pytest.fixture
def runner_unit(db_conn: psycopg.Connection) -> Iterator[None]:
    """The agent-runner unit (agent-runner): role='agent-runner', holds a
    gateway_url pointing at the gateway, no local gateway/docker.

    This is the SDK side — spawn_agent takes the local-spawn path here. Matches
    the session default role, but as an explicit opt-in so a test declares it is
    the runner unit rather than relying on the global default.
    """
    _ = db_conn  # per-test truncate side effect
    with _machine_identity(role="agent-runner"):
        yield


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
    `tests/cli/conftest.py` — a module takes it with one line:
    `pytestmark = pytest.mark.usefixtures("_installed_machine_identity")`.
    """
    from shared.machine import reset_identity

    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    reset_identity()
    yield tmp_path
    reset_identity()


@pytest.fixture
def workspace(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's agent workspace dir (the relative-path base of ava.files /
    ava.shell.run / ava.understand path mode), under the per-test unit home.

    Pins the agent id explicitly via monkeypatch instead of relying on the
    session-global `ava._boot._agent_id = 1` staying unmutated across test
    ordering (a leak through that global is exactly what the `_isolated_agent`
    monkeypatch fix in tests/ava/conftest.py guards against). The dir is NOT
    pre-created — `workspace_dir` mkdirs on first resolution, and several
    tests assert exactly that; pre-create with `.mkdir(parents=True)` when a
    test seeds files into it.
    """
    monkeypatch.setattr(ava._boot, "_agent_id", 1)
    return unit_home / "workspaces" / "1"


def _fail_on_leaked_os_jobs(session: pytest.Session) -> None:
    """Fail the run when it armed a job in the host's OS scheduler.

    The counterpart to `AVA_OS_JOBS_ENABLED=false`: the switch keeps the suite out
    of `~/Library/LaunchAgents` / the crontab / `\\Ava\\`, and this proves it did.
    A new job here means some path reached the scheduler anyway — a subprocess
    that lost the env, or a registrar added without the gate — and that job is now
    firing on the developer's machine on its own schedule, which is why this is a
    hard failure and not a warning.

    Only this suite's own homes are swept (`is_test_owned_job`); anything else new
    is reported and left alone, because it is far more likely to be an `ava start`
    the operator ran in another terminal than a leak.
    """
    leaked = sorted(host_ava_os_jobs() - _OS_JOBS_AT_START)
    if not leaked:
        return
    for job in leaked:
        if is_test_owned_job(job):
            remove_os_job(job)
    print(  # noqa: T201 — must reach the terminal; loguru output is captured
        "\nOS-SCHEDULER LEAK: this pytest run registered "
        f"{len(leaked)} job(s) on the host:\n  "
        + "\n  ".join(leaked)
        + "\nThe suite runs with AVA_OS_JOBS_ENABLED=false (see "
        "shared.os_cron.os_jobs_enabled); a job appearing anyway means a "
        "registrar bypassed the gate or a subprocess did not inherit the env. "
        "Jobs under this suite's own homes were removed; any others were left "
        "for you to check.",
        file=sys.stderr,
    )
    session.exitstatus = 1


# Peak memory above which a pytest session has stopped being a test run.
#
# A healthy serial `tests/cli` peaks around 0.2 GB, so this is ~30x headroom and
# cannot flake on a legitimately heavy test — it exists to catch the runaway
# class, where a bounded product loop loses its throttle and allocates until the
# host gives out. On 2026-07-30 that reached 26 GB on a 16 GB box, exhausted swap
# and degraded every process on it, including the prod cluster (issue #1001).
# Nothing failed; the run just got slow, and the incident was diagnosed from the
# outside. This turns the next one into a red test.
_PEAK_MEMORY_CEILING_MB = 6144


def _peak_memory_mb() -> tuple[float, str]:
    """This process's peak memory for the session, as (MB, the gauge's name).

    macOS compresses cold anonymous pages and RSS does not count them, so resident
    size understates a runaway by roughly an order of magnitude (~2 GB resident at
    21 GB footprint during the incident). `footprint(1)`'s `phys_footprint_peak` is
    the gauge that sees all of it; one subprocess, once, at session end. Linux has
    no compressor in the way, so peak RSS there is the whole story.

    Windows has neither gauge in the stdlib (`resource` is POSIX-only), and is
    neither where CI runs nor where the incident happened, so the guard reports
    nothing there rather than growing a third implementation.
    """
    if sys.platform == "win32":
        return 0.0, "unavailable"

    import resource

    if sys.platform == "darwin":
        out = subprocess.run(  # noqa: S603 — argv is the static footprint path + our own pid
            ["/usr/bin/footprint", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        units = {"B": 1e-6, "KB": 1e-3, "MB": 1.0, "GB": 1e3, "TB": 1e6}
        match = re.search(r"phys_footprint_peak:\s+([\d.]+)\s*(B|KB|MB|GB|TB)", out)
        if match:
            return float(match.group(1)) * units[match.group(2)], "phys_footprint_peak"
        # footprint(1) missing or its output reshaped: fall through to ru_maxrss,
        # which on macOS is BYTES (it is kilobytes on Linux).
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, "ru_maxrss"
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3, "ru_maxrss"


def _fail_on_runaway_memory(session: pytest.Session) -> None:
    """Fail the run when this worker's peak memory says something ran away.

    RUSAGE_SELF / this pid only: the throwaway pg and redis are children with their
    own budgets, and what this guards is in-process growth."""
    peak_mb, gauge = _peak_memory_mb()
    if peak_mb <= _PEAK_MEMORY_CEILING_MB:
        return
    print(  # noqa: T201 — must reach the terminal; loguru output is captured
        f"\nMEMORY CEILING: this pytest worker peaked at {peak_mb / 1024:.1f} GiB "
        f"({gauge}), over the {_PEAK_MEMORY_CEILING_MB / 1024:.0f} GiB ceiling in "
        "tests/conftest.py. A test suite does not need that much memory, so this is "
        "a runaway rather than a big test: look for a product retry loop bounded by "
        "wall clock whose sleep a test replaced with a no-op (issue #1001), or a "
        "recorder list a stubbed call appends to without bound. Profile with "
        "`footprint -p <pid>` per test rather than ps RSS, which under-reports on "
        "macOS by roughly 10x.",
        file=sys.stderr,
    )
    session.exitstatus = 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Session end: fail on leaked OS-scheduler jobs and on a runaway memory peak,
    then clean the tmpfs AVA_HOME. The Postgres / Redis clusters are torn down by
    the `_provisioned_db` / `_provisioned_redis` fixture finalizers (the cluster
    process + data dir are removed), so there is no database to drop here."""
    import shutil

    _fail_on_leaked_os_jobs(session)
    _fail_on_runaway_memory(session)
    shutil.rmtree(_TEST_AVA_HOME, ignore_errors=True)


@pytest.fixture
def cluster_defaults_unset(db_conn: psycopg.Connection) -> Iterator[None]:
    """Start the test from "the cluster has made no default-model choice".

    `cluster_defaults` is a seeded singleton (like cluster_pin) and is deliberately
    NOT in the per-test TRUNCATE list, and the session DB has the default-model
    migration applied — so without this a birth-config test would silently read the
    migration's seeded value. Snapshot / NULL / restore, so tests that assert the
    fall-through and tests that assert the row both start from a known state."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1")
        row = cur.fetchone()
        saved = row[0] if row else None
        cur.execute("UPDATE cluster_defaults SET llm_model = NULL WHERE id = 1")
    db_conn.commit()
    try:
        yield
    finally:
        with db_conn.cursor() as cur:
            cur.execute("UPDATE cluster_defaults SET llm_model = %s WHERE id = 1", (saved,))
        db_conn.commit()


@pytest.fixture
def db_conn() -> Iterator[psycopg.Connection]:
    """A sync psycopg.Connection on the session's test DB. Per-test isolation
    is handled by the autouse `_clean_state`, so this fixture only opens and
    closes the connection."""
    conn = psycopg.connect(settings.data_plane.db_url)
    try:
        yield conn
    finally:
        conn.close()


@pytest_asyncio.fixture
async def adb_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    """async counterpart of db_conn (the kernel's agent/db.py uses async). Cleanup
    is the autouse `_clean_state`; this fixture only opens the AsyncConnection."""
    aconn = await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
    try:
        yield aconn
    finally:
        await aconn.close()


@pytest_asyncio.fixture
async def aops_pool():
    """AsyncConnectionPool mirroring prod `agent/loop.py`'s db_pool config
    (the handle AvaContext exposes as `ops_pool`; autocommit + check_connection,
    small). Cleanup is the autouse `_clean_state`."""
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=AsyncConnectionPool.check_connection,
        open=False,
    ) as pool:
        yield pool


@pytest_asyncio.fixture
async def aredis_inbound_listener():
    """RedisInboundListener for wait_for_inbound / claim_node tests. Like prod:
    a dedicated Redis pub/sub subscription, self-reconnecting. Bound to the
    fixed pseudo-agent channel (agent_id=0); tests that park in a real wake need
    the listener's channel to match their agent, so they build their own
    per-agent listener instead. Closed on teardown."""
    from shared.redis_listener import RedisInboundListener

    listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=0)
    try:
        yield listener
    finally:
        await listener.close()


@pytest.fixture(autouse=True)
def _stub_label_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn with prompt path runs generate_label_async in gateway BackgroundTasks,
    internally calls build_chat_model which really hits the DeepSeek API — any test not explicitly
    covered would pollute network + slow down + unstable. autouse stub makes build_chat_model return a
    fake that raises RuntimeError; if a test actually triggers the spawn-with-prompt BackgroundTask, it just writes
    the error into loguru (label stays NULL, doesn't affect spawn behavior itself).

    Cases that explicitly want to test label auto-generation (`tests/gateway/test_labels.py`) themselves
    monkeypatch.setattr and write a new fake LLM, overriding this default.
    """

    def _fake_factory(_model: str, **_: object) -> object:
        class _RaiseLLM:
            async def ainvoke(self, _msgs: object) -> object:
                raise RuntimeError(
                    "label LLM disabled in tests — see tests/conftest.py:_stub_label_llm"
                )

        return _RaiseLLM()

    # Need to stub both positions:
    # - gateway.labels: historical location (re-export stub, no longer directly import build_chat_model)
    # - services.labeler.labeler: where generate_label_async actually resides
    # ImportError/AttributeError suppress modules not imported or attributes missing.
    with contextlib.suppress(ImportError, AttributeError):
        monkeypatch.setattr("ops.labels.build_chat_model", _fake_factory)
    with contextlib.suppress(ImportError, AttributeError):
        monkeypatch.setattr("services.labeler.labeler.build_chat_model", _fake_factory)


# The autouse `_stub_os_cron` that used to live here patched
# `gateway.app.register_os_cron` to a no-op. It could only ever protect the pytest
# process, and the registration that actually leaked ran in the e2e gateway
# SUBPROCESS, where no monkeypatch reaches. `AVA_OS_JOBS_ENABLED=false` (set at
# the top of this file, inherited by every child) replaces it and covers both.


# ── Agent-launch guard: no test forks a real agent process ──
#
# `gateway._agent_launch._launch_agent_process` detaches a named session
# (`ava-agent-<id>`) on the session backend to host a long-running agent. Any
# test that exercises a spawn / resurrect / restart path would fork a real agent
# process unless it stubs this out — historically ~86 byte-identical
# `monkeypatch.setattr(..., lambda _id, **_kw: None)` stubs, one per test, with
# the ever-present risk that a new spawn-touching test forgets one and silently
# forks a real agent on the dev box / CI runner.
#
# The autouse `_guard_agent_launch` makes the no-op the default: every test runs
# with the launch replaced by a recording spy. Tests that need to *assert* a launch
# happened take the `launched_agents` fixture (the same recorder list). Tests that
# need the launch to *fail* (backend-failure paths) still override with their own
# `monkeypatch.setattr(..., boom)` inside the test body — last-write-wins over the
# autouse default, restored LIFO at teardown.
#
# e2e is unaffected: it drives a real gateway uvicorn subprocess, so the spawn runs
# in that child process, not the pytest process this patch touches.


def spawn_agent(
    *,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    **kw: Any,
) -> int:
    """Test setup helper — the pre-#1236 `ops.agents.spawn_agent()` contract
    (create row + launch) as the two-phase split: `create_agent_row`
    (gateway-side, the main data-plane identity) then the guarded
    `_launch_agent_process` (runner-side; the autouse guard spy records the
    launch, and `launched_agents`-based tests rely on that record)."""
    from ops import agent_launch
    from ops.agent_spawn import create_agent_row
    from shared.machine import machine_name

    agent_id, birth_config = create_agent_row(
        spawner=spawner, machine=machine_name(), config=config, **kw
    )
    agent_launch._launch_agent_process(
        agent_id, config_overlay=config, birth_config=birth_config, confirm=False
    )
    return agent_id


class LaunchCall(NamedTuple):
    """One recorded `_launch_agent_process` call (id + the per-agent config it
    would have forwarded to the child: the explicit overlay and the birth stamp)."""

    agent_id: int
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None = None


# Stash key the autouse guard uses to hand its recorder list to `launched_agents`.
_LAUNCH_RECORDER: pytest.StashKey[list[LaunchCall]] = pytest.StashKey()

# Same, for the daemon-respawn guard -> `respawned_services`.
_RESPAWN_RECORDER: pytest.StashKey[list[tuple[str, str]]] = pytest.StashKey()


def _stub_everywhere(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str, stub: object
) -> None:
    """Replace `module.name` AND every already-imported alias of it.

    Patching only the definition site is a half-guard: `from mod import f` binds
    the function object into the importing module at ITS import time, and that
    alias is what gets called (`ops.controllers.stranded_pause` holds one for
    `unpause_local_cluster`, `services.healthchecks.frontend` one for
    `respawn_service`). Nothing is imported to find them — only modules the run
    already loaded are touched.

    **This is not the same job as reaching a name through its owning module**
    (`shared.cluster.session_name(...)` — see the rule in
    `conventions/python-conventions.md`). That convention stops a *new* frozen
    alias from being created, so the owner is the only surface and a reader can move
    modules without taking its patch out of reach. It can only apply to code this
    repo writes. This helper covers the aliases that already exist and cannot be
    converted away: a re-export facade (`ops/cluster.py`) is deliberately a frozen
    alias of every name it exposes, and the consumers that import from it at module
    top level freeze one more each. Converting the `ops.cluster*` family's internal
    reads did not shrink that set — every name guarded here is still reached through
    a facade alias by someone."""
    real = getattr(module, name)
    monkeypatch.setattr(module, name, stub)
    for mod in list(sys.modules.values()):
        if getattr(mod, name, None) is real:
            monkeypatch.setattr(mod, name, stub)


def _refuse_spawn(name: str, effect: str = "spawn a real long-running process"):  # type: ignore[no-untyped-def]
    """A stub that fails the test loudly instead of touching the real cluster. Unlike
    the recording no-ops above there is no legitimate "it happened, carry on"
    reading: a test that reaches one of these either meant to stub it or is testing
    it (and should carry the opt-out marker)."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            f"ops.cluster.{name}() would {effect}. Stub it in "
            f"the test, or mark the test @pytest.mark.real_cluster_spawn if it is the "
            f"subject under test (with subprocess.run faked)."
        )

    return _boom


@pytest.fixture(autouse=True)
def _guard_agent_launch(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Autouse safety net: every test runs with `gateway._agent_launch._launch_agent_process`
    replaced by a recording no-op spy, so a spawn-touching test that forgets to stub
    never forks a real agent process.

    Opt out with `@pytest.mark.real_agent_launch` when the test exercises the *real*
    launch (backend confirm / logs-dir / env-forward / config-overlay argv paths)."""
    if request.node.get_closest_marker("real_agent_launch"):
        return

    # Unit admission deliberately uses this pytest PID. Its canonical record
    # must not make a later CLI test appear hosted by that synthetic agent.
    # Real subprocess suites opt out above and keep their actual session tree.
    monkeypatch.setattr("agent.session_admission.run_dir", lambda: tmp_path / "admission-run")

    calls: list[LaunchCall] = []

    def _spy(
        agent_id: int,
        config_overlay: dict[str, object] | None = None,
        *,
        birth_config: dict[str, object] | None = None,
        **_kw: object,
    ) -> None:
        calls.append(LaunchCall(agent_id, config_overlay, birth_config))

    # Marker so a test can assert the guard (not the real session fork) is active.
    setattr(_spy, "_ava_test_guard", True)  # noqa: B010 — attribute probe by tests
    # `_launch_agent_process` forks a real process unless stubbed; the spy
    # covers spawn (confirm=False) and resurrect / respawn (confirm=True) alike.
    # `schedule_launch_confirm` would otherwise detach a background DB-polling
    # task per spawn (spawn's off-path launch confirm).
    monkeypatch.setattr("ops.agent_launch._launch_agent_process", _spy)
    # Resurrect authorizes and commits before detached launch. The launch spy
    # never advances the row, so its
    # matching confirm must be a no-op too. Tests of the real wait opt out with
    # `real_agent_launch`.
    monkeypatch.setattr("ops.agent_launch._wait_for_agent_claim", lambda _id, _attempt=None: None)
    monkeypatch.setattr("ops.agent_launch.schedule_launch_confirm", lambda _id, _attempt=None: None)
    request.node.stash[_LAUNCH_RECORDER] = calls


@pytest.fixture
def launched_agents(request: pytest.FixtureRequest, _guard_agent_launch: None) -> list[LaunchCall]:
    """The recorder list the autouse guard appends to. Request this when a test
    asserts a launch happened (replaces per-test `_record` stubs). Incompatible
    with `@pytest.mark.real_agent_launch` (no spy is installed then)."""
    return request.node.stash[_LAUNCH_RECORDER]


@pytest.fixture(autouse=True)
def _guard_service_respawn(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: `shared.service_respawn.respawn_service` is replaced by a
    recording no-op, so a test that reaches a healthcheck's restart path never forks
    a real long-running daemon.

    The sibling of `_guard_agent_launch`, and closing the same kind of hole one
    tier down. Daemon spawns were previously kept out of the suite purely by every
    test author remembering to monkeypatch — no structural enforcement — and a
    leaked daemon is the more expensive kind of escape: it binds a health port and
    answers /healthz, which is exactly how a pytest-spawned restarter stood in for
    prod's for 98 minutes on 2026-07-24. Port pinning at the top of this module
    keeps such a leak off prod's ports; this keeps it from happening at all.

    Patching the definition site alone would be a half-guard: `from
    shared.service_respawn import respawn_service` binds the function object into
    the importing module at ITS import time, and that alias is what gets called
    (`services/healthchecks/frontend.py` still holds one). So every already-loaded
    alias of the real function is rebound as well — no module is imported to find
    them, only the ones the test run already pulled in are touched.

    Opt out with `@pytest.mark.real_service_respawn` when the test exercises the
    real function — argv-shape assertions against a faked `subprocess.run`, or the
    venv-activation test that genuinely wants the real launch."""
    if request.node.get_closest_marker("real_service_respawn"):
        return

    calls: list[tuple[str, str]] = []

    def _spy(service: str, cmd: str, _repo: object, **_kwargs: object) -> bool:
        # Swallow the keyword-only arguments (`checkout`, `extra_env`) so the net
        # keeps standing in for the real signature as callers gain them.
        calls.append((service, cmd))
        return True

    _stub_everywhere(monkeypatch, shared.service_respawn, "respawn_service", _spy)
    request.node.stash[_RESPAWN_RECORDER] = calls


@pytest.fixture(autouse=True)
def _reset_keepalive_state() -> Iterator[None]:
    """`run_keepalive` keeps per-label state (failure count, backoff deadline +
    exponent, breaker hold) in module globals, modeling the long-lived watchdog
    process. Tests drive `main()` with fake probes, so without a reset one test's
    failed respawn leaves a backoff window that silently skips the next test's
    respawn — the shard-6/8 failure of task #1941's PR. Reset around every test;
    the per-test cost is four dict clears under one lock."""
    with shared.service_respawn._keepalive_state_lock:
        shared.service_respawn._consecutive_probe_failures.clear()
        shared.service_respawn._respawn_attempts.clear()
        shared.service_respawn._next_respawn_at.clear()
        shared.service_respawn._breaker_hold_since.clear()
    yield


@pytest.fixture(autouse=True)
def _guard_cluster_spawn(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Autouse safety net: the `ops.cluster` entry points that detach a session and
    leave something long-running behind are stubbed.

    `unpause_local_cluster` spawns a real `services.restarter.daemon`; the three
    `spawn_*` functions detach a session that runs `ava cluster update` against this
    checkout. Reaching any of them for real from a test is never intended — and it
    was reachable: `tests/cli/test_update_autounpause.py` drives
    `_run_gateway_orchestration`, whose `finally` unpauses the LOCAL host, and its
    stubs covered only the remote fan-out. That spawned a live `ava-restarter`
    every run. Before this change the daemon landed on the operator's default
    session backend bound to prod's health port, which is the 2026-07-24 outage exactly.

    Opt out with `@pytest.mark.real_cluster_spawn` where the function under test IS
    one of these (those tests fake `subprocess.run`, so nothing is really spawned).

    The teardown also clears the `cluster_paused` flag, which is per-machine FILE
    state in the test home and therefore outlives the test that set it. While it
    exists the pause middleware answers 503 to every request, so one test that
    pauses and does not unpause fails every later `TestClient` test in the session
    — hundreds of them, in whatever file happens to run next, with nothing
    pointing back at the culprit."""
    import ops.cluster

    if not request.node.get_closest_marker("real_cluster_spawn"):
        for name in ("unpause_local_cluster", "spawn_update", "spawn_rollout", "spawn_restart"):
            _stub_everywhere(monkeypatch, ops.cluster, name, _refuse_spawn(name))
        # The one guarded entry point that DESTROYS rather than spawns: it runs on
        # every agent-runner watchdog round, so any test that drives the default
        # controller list reaches it, and its whole job is to force-kill a live
        # `ava-updater`. Same guard, opposite direction.
        _stub_everywhere(
            monkeypatch,
            ops.cluster,
            "reap_stalled_updater_if_hung",
            _refuse_spawn(
                "reap_stalled_updater_if_hung", effect="force-kill a real ava-updater session"
            ),
        )
        # Its gateway-side sibling, guarded for the same reason and harder: it reads
        # THIS host's `cluster_last_update` row, and on a dev box that row is prod's
        # (a worktree's `db_url` is the central database). A test that drove the
        # default controller list with a gateway role would send a real SIGINT to a
        # real prod rollout's pid.
        import ops.controllers.stalled_rollout

        _stub_everywhere(
            monkeypatch,
            ops.controllers.stalled_rollout,
            "reclaim_stalled_rollout_if_hung",
            _refuse_spawn(
                "reclaim_stalled_rollout_if_hung",
                effect="SIGINT or force-kill a real ava-rollout orchestration",
            ),
        )
    yield
    # The pause state is the host_deploy_state posture row — per-test TRUNCATE
    # clears it; the retired flag files need no cleanup.


@pytest.fixture
def respawned_services(
    request: pytest.FixtureRequest, _guard_service_respawn: None
) -> list[tuple[str, str]]:
    """The `(service, cmd)` pairs the guard recorded. Request this to assert a
    healthcheck tried to respawn something."""
    return request.node.stash[_RESPAWN_RECORDER]


@pytest.fixture(autouse=True)
def _guard_schedule_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: the gateway lifespan's ScheduleManager touches the real
    session backend (reconcile `list_sessions`/`new_session`; the API
    `sync`/`capture` control paths). Under `TestClient(app)` that would hit the
    real backend and pollute tests recording `subprocess.run`. Neutralize the
    session-touching methods (`start` background task, plus `sync`/`capture`
    invoked by the routes); tests that exercise the real reconcile/sync logic
    call the manager's blocking methods on an instance directly with a faked
    backend."""

    async def _noop_start(self: object) -> None:
        return None

    async def _noop_sync(self: object, schedule_id: int) -> None:
        return None

    async def _noop_capture(self: object, schedule_id: int, lines: int) -> None:
        return None

    monkeypatch.setattr("gateway.schedule_manager.ScheduleManager.start", _noop_start)
    monkeypatch.setattr("gateway.schedule_manager.ScheduleManager.sync", _noop_sync)
    monkeypatch.setattr("gateway.schedule_manager.ScheduleManager.capture", _noop_capture)


# ── Warm-up guard: no test fires the detached `ava start` warm-up subprocess ──
#
# `ava start` calls `cli.commands._launch_agent_warmup`, which Popens a detached
# `agent.warmup` (via sys.executable) on agent-runner hosts. Tests that drive the
# real `cmd_start` body (role agent-runner) would fork that heavy process unless
# it is stubbed. Same safety-net pattern as `_guard_agent_launch` above.


@pytest.fixture(autouse=True)
def _guard_agent_warmup(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: every test runs with `cli.commands._launch_agent_warmup`
    replaced by a no-op, so a start-touching test never forks the warm-up process.

    Opt out with `@pytest.mark.real_warmup_launch` when the test exercises the
    real launcher (role gate / Popen argv / detach)."""
    if request.node.get_closest_marker("real_warmup_launch"):
        return
    monkeypatch.setattr("cli.commands._launch_agent_warmup", lambda *_a, **_kw: None)


# ── Gateway-readiness guard: no orchestration test dials a real gateway ──
#
# The rollout waits for its own gateway to serve before Phase B fans out
# (`cli.commands._gateway_ready`). Under pytest that dial can only fail — there is no
# gateway — and it would fail *slowly enough to matter* and turn every Phase-B test's
# rc into 1. Stub it to SERVING by default so an orchestration test asserts what it is
# about; the gate's own behaviour is covered by tests/cli/test_phase_b_gateway_ready.py,
# which opts out. Same safety-net pattern as the guards above.


@pytest.fixture(autouse=True)
def _guard_service_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `cli.commands._wait_for_services_ready` reports every
    service ready without polling anything.

    The start path's readiness wait is bounded by `SERVICE_READY_TIMEOUT_S` (180 s),
    and a test that reaches it with real probes would sit there for the full bound —
    long enough under `-n auto` for the worker to be declared down, which reports as
    an infrastructure failure rather than as the stubbing gap it is. Reporting no
    unready specs also keeps such a test's `ava start` at rc 0 instead of the
    readiness code.

    Opt out with `@pytest.mark.real_service_readiness_gate` when the wait or the exit
    code it produces is the subject (tests/cli/test_start_readiness_gate.py)."""
    if request.node.get_closest_marker("real_service_readiness_gate"):
        return
    from cli.commands import ReadinessWait

    monkeypatch.setattr(
        "cli.commands._wait_for_services_ready",
        lambda *_a, **_kw: ReadinessWait((), 0.0, sessions_gone=False),
    )


@pytest.fixture(autouse=True)
def _guard_health_port_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `ava start`'s pre-bind health-port gate finds nothing.

    The gate dials this unit's daemon `/healthz` ports before launching (issue
    #977). It does NOT reach the prod defaults 8102-8111: the env block at the top
    of this file already pins every health port to a `_free_port()`, so an
    un-stubbed gate dials this session's own kernel-assigned ports and the suite is
    green with prod live. This fixture buys the weaker, residual property.

    `_free_port()` releases the port before anything binds it — the same race
    `tests/_containers.py` accepts for pg/redis. Nothing in a test run ever binds
    these particular ports, so the window is the whole session, and an unrelated
    process that grabs one and answers 2xx on `/healthz` would be read as
    PORT_TAKEN: a start test failing for a reason no test caused. Rare, but the
    stub costs one line and removes the flake source entirely.

    Opt out with `@pytest.mark.real_health_port_gate` when the gate itself is the
    subject (tests/cli/test_start_health_port_gate.py)."""
    if request.node.get_closest_marker("real_health_port_gate"):
        return
    monkeypatch.setattr("cli.commands._occupied_health_ports", lambda *_a, **_kw: ())


@pytest.fixture(autouse=True)
def _guard_force_kill_confirmation() -> None:
    """Autouse no-op since S7: the force-kill confirmation wait lived in the
    deleted backend (`_force_kill` re-asked `has-session` for up to
    `_FORCE_CONFIRM_TIMEOUT_S`, issue #1015). The native supervisors confirm a
    kill by re-checking the process synchronously — no patience to shrink."""


@pytest.fixture(autouse=True)
def _guard_gateway_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autouse safety net: `cli.commands._await_gateway_serving` answers SERVING
    without dialing anything.

    Opt out with `@pytest.mark.real_gateway_readiness_gate` when the gate itself is the
    subject."""
    if request.node.get_closest_marker("real_gateway_readiness_gate"):
        return
    from cli.commands._gateway_ready import GatewayReadiness

    monkeypatch.setattr(
        "cli.commands._await_gateway_serving",
        lambda **_kw: (GatewayReadiness.SERVING, "stubbed"),
    )


# ── Exec guard: no test replaces the pytest process image ──
#
# `os.exec*` does not raise, does not unwind, and does not run a finalizer — it
# overwrites this process with a new program. A test that reaches one takes the
# whole run with it: pytest never reaches `pytest_sessionfinish`, never prints a
# summary, and the shell sees whatever the *replacement* program exited with.
# There is no failure report, so a full-suite run silently under-reports every
# test that had not run yet.
#
# That is not hypothetical (pre-2026-08-01): `cli.start_refresh.
# refresh_runner_env_or_die` ran from `cli.main.main()` before dispatch on
# `ava start`, and on an enrolled runner it fetched /api/bootstrap, rewrote the
# real ~/.ava/.env, and re-exec'd — replacing the pytest process mid-run.
# `tests/cli/test_main_dispatch.py` stubbed it, and the exec guard here is the
# backstop that turns a forgotten stub into a plain test failure instead of a
# vanished run. (The refresh is gone; `ava start`'s Settings build fetches in-
# process, which is what `_guard_bootstrap_fetch` below blocks.)
#
# Sibling shape to watch for: the guard is what makes the next one a plain test
# failure instead of a vanished run.


@pytest.fixture(autouse=True)
def _guard_process_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: the `os.exec*` family fails the test loudly instead of
    replacing the pytest process.

    Patching the four `execv*` names covers all eight — stdlib `os.execl*` are
    thin wrappers that call the module-global `execv`/`execvp`. Real in-process
    exec is never intended from a test: the production call sites are either a
    dedicated `__main__` that runs in a subprocess (`shared._reparent`,
    `services.browser.daemon`, `services.milvus.daemon`) or a CLI re-exec. Tests
    that assert an exec *would* have happened patch `os.execv` themselves inside
    the test body (`tests/cli/test_start_refresh.py`) — last-write-wins over this
    default, restored LIFO at teardown.

    `os._exit` is deliberately NOT guarded: it is the correct call in a forked
    child (`shared._reparent`), and hijacking it there would resurrect a pytest
    process inside the fork.
    """

    def _refuse(name: str):  # type: ignore[no-untyped-def]
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                f"os.{name}() would replace the pytest process image — the run would end "
                f"here with no summary and no failure report. Stub the code path that "
                f"execs (see tests/cli/test_main_dispatch.py), or patch os.{name} in the "
                f"test body if the exec is the subject under test."
            )

        return _boom

    for _name in ("execv", "execve", "execvp", "execvpe"):
        monkeypatch.setattr(os, _name, _refuse(_name))


@pytest.fixture(autouse=True)
def _guard_bootstrap_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: no test performs a real `GET /api/bootstrap` against a
    live gateway.

    The guard above stops an exec, but the fetch happens first: a pure
    agent-runner's Settings build calls `inject_config_from_gateway()` at
    import, and on an enrolled dev box that is a live GET /api/bootstrap
    against the real cluster gateway. This guard sits at the cause. Enroll
    also funnels through `shared.bootstrap.fetch_bootstrap_config` (the only
    caller of this module's `dial_get`); nothing is written until that fetch
    returns, so refusing the dial blocks the whole chain: no request to the
    gateway, no enroll state.

    That chain is not theoretical. On an enrolled runner — serve_gateway unset,
    gateway URL in the real `~/.ava/.env`, which is every dev box joined to a
    cluster — a test that constructs Settings without the suite's
    AVA_CONFIG_FETCH=skip pin would fetch from that cluster's live gateway.
    The suite redirects `AVA_HOME` to tmpfs and pins the fetch skip in the
    env block above; this guard is the second net for any test that drives a
    Settings build directly.

    Narrow by construction: `shared.bootstrap` binds `dial_get` at module level,
    so only the bootstrap egress is touched — the ~30 other `shared.http_dial`
    call sites are untouched. Every test that legitimately drives the fetch
    already substitutes its own transport at exactly this seam
    (`tests/shared/test_bootstrap_fetch.py` routes it through an in-process
    TestClient; the retry tests hand it a fake), and those patches win by LIFO.
    """
    import shared.bootstrap

    def _boom(url: object = "", *_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            f"a real GET {url} would leave the test process — this is the call that "
            "reaches a live gateway and, on an enrolled runner, rewrites the operator's "
            "~/.ava/.env. Stub the caller, or patch shared.bootstrap.dial_get in the test "
            "body with a fake transport (see tests/shared/test_bootstrap_fetch.py)."
        )

    monkeypatch.setattr(shared.bootstrap, "dial_get", _boom)


@pytest.fixture(scope="session")
def milvus_server() -> Iterator[str]:
    """Session-scoped standalone milvus-lite server.

    Spawns `milvus-lite server --data-dir <tmp> --port <random>` once per pytest
    session, shared by all memory_indexer / ava.memory tests. URI exposed via fixture,
    tests use `monkeypatch.setenv("AVA_MILVUS_URI", ...)` to let `index.connect()` connect
    to this subprocess instead of prod default 19530.

    Consistent with prod using standalone server —— not mixing lite-in-process, avoiding behavior drift.
    Spawn cost ~3s once per session; tests not referencing this fixture (e.g. fast subset)
    don't trigger spawn.
    """
    import shutil
    import socket
    import subprocess
    import sys
    import tempfile
    import time

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    tmpdir = Path(tempfile.mkdtemp(prefix=f"ava_milvus_test_{port}_"))
    # `.venv/bin/milvus-lite` —— Python sys.executable is in .venv/bin/python, binary in same dir.
    # Use absolute path not dependent on whether caller uses `uv run` (PATH might not have .venv/bin/).
    binary = Path(sys.executable).parent / "milvus-lite"
    proc = subprocess.Popen(  # noqa: S603 — binary is neighbor of sys.executable, args hardcoded
        [
            str(binary),
            "server",
            "--data-dir",
            str(tmpdir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    uri = f"http://127.0.0.1:{port}"
    # Wait for server listen —— TCP probe up to 10s, milvus-lite startup needs 1-3s.
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"milvus-lite server failed to start on :{port} within 10s")

    yield uri

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


_MILVUS_TEST_DIM = 8
"""Test vector width — small, matches the embedding dummy vectors."""
_MILVUS_TEST_FP = "test:gemini:dim=8"
"""Test provider fingerprint — stamped on rows, compared by reconcile tests."""


@pytest.fixture
def milvus_client(milvus_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Fresh MilvusClient connected to session server, after test drop collection for isolation.

    Each test gets a clean collection —— `MilvusBackend(dim=..., fingerprint=...)`'
    connect() idempotently creates it, yields the underlying client,
    teardown drops so next test sees empty collection.
    """
    # Settings module-loaded once BaseSettings, setenv then settings.services.milvus_uri
    # does not re-read env. Directly monkeypatch.setattr change Settings instance field, consistent with
    # tests/ava/test_web.py and other monkeypatch patterns.
    from shared.config import settings

    monkeypatch.setattr(settings.services, "milvus_uri", milvus_server)
    from services.memory_indexer.backends.milvus import _COLLECTION, MilvusBackend

    backend = MilvusBackend(dim=_MILVUS_TEST_DIM, fingerprint=_MILVUS_TEST_FP)
    backend.connect()
    client = backend._require_client()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            # pyright infers pymilvus client.has_collection as coroutine (stub noise),
            # actually sync bool. Keep suppress to avoid reportUnnecessaryComparison false positive.
            if client.has_collection(_COLLECTION):  # pyright: ignore[reportUnnecessaryComparison]
                client.drop_collection(_COLLECTION)
        with contextlib.suppress(Exception):
            client.close()


@pytest.fixture
def loguru_records() -> Iterator[list[dict]]:
    """Capture loguru output for tests. pytest's built-in caplog captures stdlib logging,
    loguru doesn't go through stdlib, requires separate bridging.

    Usage:
        def test_foo(loguru_records):
            do_something_that_logs()
            assert any("expected" in r["message"] for r in loguru_records)
    """
    from loguru import logger as _logger

    captured: list[dict] = []
    handler_id = _logger.add(lambda msg: captured.append(dict(msg.record)), level="DEBUG")
    try:
        yield captured
    finally:
        _logger.remove(handler_id)


@pytest.fixture(autouse=True)
def _no_stdlib_telemetry_bridge() -> Iterator[None]:
    """Keep stdlib logging off the loguru->telemetry bridge during tests.

    shared/log.py's `_install_stdlib_intercept()` (called by init_gateway_process
    and friends) installs a root-logger handler that forwards stdlib records
    into loguru, whose `_postgres_sink` turns them into telemetry 'log' events.
    Any test that monkeypatches `shared.telemetry.emit` and triggers a log
    record then sees the bridge's forwarded 'log' event in its captured events
    (pgbouncer healthcheck tests, fixed 2026-08-09 via #2136). Tests that
    exercise the bridge itself re-install it inside their body
    (test_uvicorn_stdlib_intercept.py): the fixture runs first, the body's
    install wins for the duration, and teardown restores the saved handlers.
    """
    import logging

    from shared.log import _StdlibInterceptHandler

    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = [h for h in saved if not isinstance(h, _StdlibInterceptHandler)]
    yield
    root.handlers = saved
