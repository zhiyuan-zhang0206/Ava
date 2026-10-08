"""Import-time environment isolation for the whole test session (the first plugin).

Loaded first by the repo-root `conftest.py`, and it has to stay first: everything
here runs at IMPORT time, before any project module exists. It redirects
`AVA_HOME` to a per-session tmpfs home, pins every cluster-scope value to a test
sentinel, scrubs the operator's ambient environment, builds the Settings
singleton, and then pins the session-wide runtime state on it (agent id, machine
files, service ports, fd limit). `_assert_env_precedes_project_imports` fails the
run when a project module was imported earlier, i.e. when a plugin that imports
project code was listed ahead of this one in the root `conftest.py`.

`tests.fixtures.provisioning` (the throwaway databases and the session hooks)
and every other plugin load after this module and read what it established.
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
import socket
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

# Third-party only. These import no project module, so they are safe above the
# env block; every `base.*` / `ava.*` import is below it. `tests._test_env_file`
# is the one project-side import above it: it imports no project module either,
# and importing it pops the host's AVA_LGTM_* variables before Settings exists.
import pytest

import tests._test_env_file  # noqa: F401  # pyright: ignore[reportUnusedImport]  # side effect is the point

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
# provisions its own throwaway postmaster via `pg_ctl` (base/cluster/dataplane/pg_tools.py),
# which inherits this process's environment, so pinning LC_ALL here — above
# every project import and any postmaster spawn — fixes every run on a box
# whose shell (session backend / launchd / CI) never set it. Only macOS needs this;
# Linux CI ships its own locale and would fail on a missing en_US.UTF-8.
if sys.platform == "darwin":
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")

# ── The unit's home: redirected here, before anything can read it ──
#
# `base.host.env.dotenv_boot.resolve_ava_home` reads `AVA_HOME` (else `~/.ava`)
# every time it is asked; nothing captures it at import. What this block must
# still precede is `Settings`: it reads about twenty-five other variables at
# import, and its boot (`load_ava_env`) loads `$AVA_HOME/.env` and force-assigns
# every derived_env_keys() entry (`os.environ[key] = val`, overriding what is
# already there). With the home left at the operator's `~/.ava`:
#
#   - `load_ava_env()` reads the operator's REAL `~/.ava/.env`, so the
#     production AVA_CLUSTER_SECRET, AVA_DB_URL and AVA_REDIS_URL land in the
#     test process, overwriting the sentinels set a few lines below. A test
#     suite holding production credentials is the real severity here.
#   - every Settings read (`Settings` field defaults, the gateway config fetch)
#     would follow the operator's cluster; the suite pins AVA_CONFIG_FETCH=skip
#     (below) so no Settings construction may dial a gateway.
#   - the CLI's `.env` writers resolve the home from the same variable, so
#     `tests/cli` would rewrite the operator's real `~/.ava/.env` (that happened
#     three times on one dev box in a single night, 2026-07-28/29, during a
#     production rollout).
#
# The general rule, and it predicts the next one: the suite isolates everything
# addressed by a value a process reads AT USE TIME, and leaks everything fixed
# BEFORE isolation takes effect. Two ways to be fixed-before: a value bound at
# import (Settings' own snapshot, which this block precedes) and a host-global
# namespace with no value to redirect at all (the scheduler, below — same
# lesson, no fix available beyond not acting). The variable is exported, not
# set on a fixture, so the subprocesses tests spawn (`ava` CLI, e2e gateway)
# inherit the same home.
_SESSION_SUFFIX = f"{os.getpid()}_{int(time.time() * 1_000_000)}"
_TEST_AVA_HOME = Path(tempfile.mkdtemp(prefix=f"ava_test_home_{_SESSION_SUFFIX}_"))
os.environ["AVA_HOME"] = str(_TEST_AVA_HOME)
# CI keeps the vendored Postgres tree (zonky + pgvector, put in place by
# scripts/ci/vendor_pg_runtime.py) outside the per-session home; linking it in
# makes `pg_tool` resolve the production server build instead of a host apt one.
_CI_VENDORED_RUNTIME_ROOT = os.environ.get("CI_VENDORED_RUNTIME_ROOT")
if _CI_VENDORED_RUNTIME_ROOT:
    (_TEST_AVA_HOME / "runtime").symlink_to(_CI_VENDORED_RUNTIME_ROOT, target_is_directory=True)
# Persist the same worker-local channel through settings refresh and child boot.
# A mutation of settings.data_plane alone is lost when a rollout rebuilds it.
_TEST_EVENTS_CHANNEL = f"ava:events:test:{_SESSION_SUFFIX}"
os.environ["AVA_EVENTS_CHANNEL"] = _TEST_EVENTS_CHANNEL

# No Settings construction in the suite may fetch from a gateway. The config
# source is role-derived (AVA_CONFIG_SOURCE is gone; a unit that does not serve
# the gateway fetches at Settings build), and this test process is
# agent-runner-only by default — without the skip, `import ava` below would make
# a live GET /api/bootstrap against whatever AVA_GATEWAY_URL leaks in. The
# suite's cluster-scoped values (db/redis URLs, secret) are pinned explicitly in
# this env block; subprocess tests that spawn real daemons/agents re-derive or
# re-pin their own env per test.
os.environ["AVA_CONFIG_FETCH"] = "skip"

# ── Pre-compact history dump: pinned OFF for the suite, like a cluster that
# configures the flag off ──
#
# The flag defaults ON (2026-09-11); left on, every compaction path would write
# a JSONL dump into the test home and inject the dump note after the summary.
# compact-behavior tests assert exact post-compact tails, and test_config
# compares fresh Settings instances against the singleton under "the same env"
# — so every layer must agree on the value: the singleton built below, the D5
# pre-warmed fallback, fresh instances, and spawned subprocesses. A fixture-time
# attribute patch reaches only the singleton (the D5 pre-warm snapshots at
# import) — pin the environment before the first Settings construction instead,
# and declare the same key in the test home's .env below so the cluster-env
# authority pass takes its force branch (an undeclared cluster-scope key is
# DROPPED). Feature tests (agent/tests/history/test_history_dump.py) flip the singleton
# back on per test.
os.environ["AVA_COMPACT_HISTORY_DUMP"] = "false"

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

# ── Boot mode: the suite exercises the eager config chain ──
#
# `import base.config` boots the boot-lite state by default (lazy v2, task
# #3621): the boot-path fields resolve from the generated index without
# constructing Settings, and the eager chain builds on the first touch of
# anything else. The suite's fixtures and assertions (Settings construction,
# `model_fields_set` probes, the metadata walks) assume the eager chain from
# import time, so pin it here; the lite paths are exercised in their own
# subprocess tests (base/config/tests/test_config_boot_lite.py). Read at
# `base.config` import, hence inside this block.
os.environ.setdefault("AVA_CONFIG_BOOT", "eager")

# ── OS-scheduled jobs: the suite never arms one ──
#
# launchd reads ONE ~/Library/LaunchAgents per OS user; `crontab` edits ONE table
# per user. Unlike AVA_HOME / the DB /
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
# `base.host.system.cron.os_jobs_enabled` gates all four registrars on this; the
# unregister paths stay live so cleanup still works.
os.environ["AVA_OS_JOBS_ENABLED"] = "false"

# Import-time sentinel for the required-no-default Settings fields (db_url /
# redis_url). Set BEFORE base.config is imported so Settings() constructs from
# the sentinel even with no AVA_DB_URL in the environment (CI / a fresh clone),
# and a stray connection that bypasses a provisioning fixture fails loudly
# instead of hitting a real database. load_dotenv(override=False) won't clobber
# these. The real per-session URLs are injected by the `_provisioned_db` /
# `_provisioned_redis` fixtures, which start throwaway native pg/redis (tests/_containers.py).
os.environ["AVA_DB_URL"] = "postgresql://unprovisioned@127.0.0.1:1/unprovisioned"
os.environ["AVA_REDIS_URL"] = "redis://127.0.0.1:1/0"

# The cluster secret authenticates bootstrap / /ops. Data-plane credentials
# are independent, including in the private test bootstrap. Use a URL-safe token that
# passes the cluster_secret validator. Individual auth tests monkeypatch it (incl.
# to "" for the unset-fail-closed paths).
os.environ["AVA_CLUSTER_SECRET"] = "test-cluster-secret"  # noqa: S105 — test fixture
# The suite's secret-bearing servers include the e2e ops daemon, which binds
# non-loopback. The deployment precondition requires a declared mode; overlay
# records the suite's private-network posture.
os.environ["AVA_TRANSPORT_ENCRYPTION"] = "overlay"

# The suite's data-plane identity is `ava_citest`, carried entirely by the URLs
# the provisioning fixtures write (names-as-data — Settings keeps the URL
# verbatim, credentials included). The throwaway pg/redis provide the
# `ava_citest` role/db/ACL user (tests/_containers.py), while the prod-db guard
# (tests/path_scoped/ava_tests.py, which refuses `ava`/`ava_main`) still fires if a test
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
# (base/telemetry/otlp/tests/test_telemetry_otlp.py) re-enable the flag and install
# in-memory providers where the path is under test.
os.environ["AVA_TELEMETRY_OTLP_ENABLED"] = "false"

# ── Host-scope identity and telemetry URLs: loopback, like CI ──
#
# The native LGTM installer renders Tempo targets from the host-scope
# telemetry settings (`settings.observability.telemetry_tempo_query_url` /
# `telemetry_tempo_endpoint`), and `_self_machine_host()` prefers the
# AVA_MACHINE_HOST env var. The login
# shell exports the operator's real ~/.ava/.env into every child process (the
# 2026-08-04 shell-env leak class), and host-scope keys survive the
# cluster-scope drop in `_enforce_cluster_env_authority` — so on a dev box a
# private-network host override reached the suite and
# `test_ensure_renders_configs_with_native_paths_and_loopback` rendered
# `http://100.x.y.z:3200` instead of loopback while CI (no .env) stayed green.
# Pinned unconditionally, like the gateway/telegram sentinels: local runs
# resolve the same loopback host everywhere, and a test that needs a remote
# Tempo URL or host address monkeypatches the settings explicitly
# (base/cluster/tests/test_machine.py does for machine_host).
os.environ["AVA_TELEMETRY_TEMPO_QUERY_URL"] = "http://127.0.0.1:3200"
os.environ["AVA_TELEMETRY_TEMPO_ENDPOINT"] = "http://127.0.0.1:14318"
# The OTLP ingress port is rendered into the collector config, the roster gate
# and the healthcheck probe ports; an ambient AVA_TELEMETRY_OTLP_PORT from the
# operator's .env would move every rendered endpoint off the pinned 4318 the
# render tests assert. Pinned for the same leak class as the Tempo URLs.
os.environ["AVA_TELEMETRY_OTLP_PORT"] = "4318"
# The collector endpoint is a value AND a signal: any env value marks the
# field explicitly set (killing the port-derived default) and opens the OTLP
# export gates for every identity (endpoint_override_is_explicit). So unlike
# the pins around it, an ambient value cannot be neutralized by pinning a
# loopback default — a box's .env (its host-port block on 4319) would still
# move derived endpoints and flip gate outcomes, while CI (no such variable)
# stayed green (test_exec_subprocess.py, test_otel_bootstrap_relay.py,
# 2026-09-14). Popped, never set: absent is the state CI runs in — the
# endpoint derives from the pinned port, and a test that needs an explicit
# endpoint monkeypatches it. Host AVA_LGTM_* are popped in tests/_test_env_file.
os.environ.pop("AVA_TELEMETRY_OTLP_ENDPOINT", None)
os.environ["AVA_MACHINE_HOST"] = "localhost"

# ── Grafana admin credential: the suite never carries a live one ──
#
# Same leak class: the login shell exports the real GRAFANA_ADMIN_PASSWORD,
# and the native LGTM render writes `settings.alerts.grafana_admin_password`
# into the rendered native dir — a production credential flowing through test
# output. Pinned empty like the telegram token; a test that needs a credential
# monkeypatches `settings.alerts.grafana_admin_password`
# (cli/commands/observability/tests/test_converge_lgtm.py does).
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
# `settings.telegram` explicitly (cli/commands/cluster/tests/test_cluster_health.py does).
os.environ["AVA_TELEGRAM_BOT_TOKEN"] = ""
os.environ["AVA_TELEGRAM_OWNER_ID"] = "0"
# Terminal/execution transport tests have no inherited desktop-helper route.
# This switch does not disable mandatory macOS application-root ancestry:
# _guard_permissions_helper_native_io forbids those native effects separately.
# A terminal transport test can deliberately patch its own helper capability.
os.environ["AVA_PERMISSIONS_HELPER_SPAWN"] = "false"
# The spawn-attribution marker rides a second channel the pin above cannot
# close: the signed helper stamps AVA_PERMISSIONS_HELPER_PID (its own pid)
# into every direct child (services/desktop/permissions_helper/helper/main.swift),
# descendants inherit it, and base/helper_chain_guard.parent_chain_intact
# treats a marked process whose ancestor chain lacks the helper as an
# orphaned child — the agent-host heartbeat then self-terminates with os._exit(70),
# killing an in-process test run (test_host_turn_progress_publish.py,
# 2026-09-12; 4 passed then rc=70). Popped, never set empty: an empty value
# is a MALFORMED marker, i.e. a broken chain. PORT (which helper instance)
# is stripped with it so the suite carries no helper spawn context at all —
# the state CI runs in. A test that needs the marker sets it via monkeypatch
# (tests/harness/test_helperproc.py).
os.environ.pop("AVA_PERMISSIONS_HELPER_PID", None)
os.environ.pop("AVA_PERMISSIONS_HELPER_PORT", None)
# ── Off-site backup destination: no ambient AVA_BACKUP_OFFSITE_* in the suite ──
#
# Same shell-leak class as the telegram token above. A field the fixture `.env`
# does not pin falls back to the BOOT-TIME environment value (candidate
# validation reconstructs a domain from the captured `.env` image and the patch),
# and a pytest run inside a process that carries the production destination would
# resolve an "unconfigured" home as configured. CI carries no such env; scrubbing
# here makes a local run resolve exactly as CI does. A test that exercises the
# destination sets its own keys (monkeypatch / write_fields).
for _offsite_key in [key for key in os.environ if key.startswith("AVA_BACKUP_OFFSITE_")]:
    del os.environ[_offsite_key]
# The WAL-G switch is the same class: a shell that carries it would turn archiving
# on for every Postgres a test starts. The key is off by default and a test that
# needs it sets it itself.
os.environ.pop("AVA_WALG_CONFIG_FILE", None)

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
            # Cluster-scope pin carried by both channels (see the block above):
            # the env side sets it before Settings construction, the file side
            # keeps the authority pass on its force branch.
            f"AVA_COMPACT_HISTORY_DUMP={os.environ['AVA_COMPACT_HISTORY_DUMP']}",
            f"AVA_DB_URL={os.environ['AVA_DB_URL']}",
            f"AVA_REDIS_URL={os.environ['AVA_REDIS_URL']}",
            f"AVA_EVENTS_CHANNEL={_TEST_EVENTS_CHANNEL}",
            f"AVA_CLUSTER_SECRET={os.environ['AVA_CLUSTER_SECRET']}",
            "AVA_RUNNER_DB_PASSWORD=test-runner-db-password",
            f"AVA_TRANSPORT_ENCRYPTION={os.environ['AVA_TRANSPORT_ENCRYPTION']}",
            f"AVA_GATEWAY_URL={os.environ['AVA_GATEWAY_URL']}",
            f"AVA_TELEGRAM_BOT_TOKEN={os.environ['AVA_TELEGRAM_BOT_TOKEN']}",
            f"AVA_TELEGRAM_OWNER_ID={os.environ['AVA_TELEGRAM_OWNER_ID']}",
            *(f"{key}={os.environ[key]}" for key in _TEST_PROVIDER_KEY_ENVS),
            # Host binary selections are file-owned. Preserve an explicit test
            # override through Settings refresh without importing a real home.
            *(
                f"{key}={os.environ[key]}"
                for key in ("AVA_REDIS_BIN_DIR", "AVA_PG_BIN_DIR")
                if key in os.environ
            ),
            f"AVA_TELEMETRY_OTLP_ENABLED={os.environ['AVA_TELEMETRY_OTLP_ENABLED']}",
            f"AVA_TELEMETRY_TEMPO_QUERY_URL={os.environ['AVA_TELEMETRY_TEMPO_QUERY_URL']}",
            f"AVA_TELEMETRY_TEMPO_ENDPOINT={os.environ['AVA_TELEMETRY_TEMPO_ENDPOINT']}",
            f"AVA_MACHINE_HOST={os.environ['AVA_MACHINE_HOST']}",
            # The session unit's identity, as an initialized home records it: spawn_agent /
            # claim_agent_row read machine_name(), and a runner flag with a gateway URL is a
            # configured agent-runner. Subprocess tests that spawn the real CLI read it here.
            f"AVA_MACHINE_NAME=test-{_SESSION_SUFFIX}",
            "AVA_MACHINE_SERVE_AGENT_RUNNER=true",
            f"GRAFANA_ADMIN_PASSWORD={os.environ['GRAFANA_ADMIN_PASSWORD']}",
            # Same force-branch reasoning: exec timeout is cluster-scoped; without a
            # declaration the drop deletes the pinned 60s and the D5 fallback reads the
            # field default (300s) — test_config bootstrap/panel tests assert equality.
            "AVA_EXEC_TIMEOUT_SECONDS=60.0",  # the pinned value below; constant, not env-read (write precedes the pin)
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
    excluded (it is the package this module lives in); `base` is the one that
    matters, since it is what loads `$AVA_HOME/.env`.
    """
    leaked = sorted(
        name
        for name in sys.modules
        if name.split(".")[0] in {"base", "ava", "agent", "gateway", "cli", "ops", "services"}
    )
    if leaked:
        raise RuntimeError(
            "tests/fixtures/env_bootstrap.py: a project module was imported before the env block "
            f"finished: {leaked[:5]}{'...' if len(leaked) > 5 else ''}. Everything above "
            "this line sets env vars that are read at IMPORT time — AVA_HOME decides "
            "which .env the config boot loads. An import above it silently pins the "
            "suite to the operator's real ~/.ava, production credentials included. Move the import below this assertion, or "
            "list the plugin that imports it after tests.fixtures.env_bootstrap in the "
            "repo-root conftest.py."
        )


_assert_env_precedes_project_imports()

# ava / base.config read AVA_DB_URL + AVA_HOME at import — must come after the
# env block above, which is what the assertion just enforced.
from base.config import set_field, settings
from base.daemon.health import _HEALTH_PORT_OVERRIDES
from base.host.env.port_table import FIXED_PORTS

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
assert "AVA_TELEMETRY_OTLP_ENDPOINT" not in os.environ
assert os.environ.get("AVA_MACHINE_HOST") == "localhost"
assert os.environ.get("GRAFANA_ADMIN_PASSWORD") == ""
assert settings.observability.telemetry_tempo_query_url == "http://127.0.0.1:3200"
assert settings.observability.telemetry_tempo_endpoint == "http://127.0.0.1:14318"
assert settings.observability.telemetry_otlp_port == 4318
assert settings.observability.telemetry_otlp_endpoint == "http://127.0.0.1:4318"
assert settings.general.machine_host == "localhost"
assert settings.general.machine_name == f"test-{_SESSION_SUFFIX}"
assert settings.alerts.grafana_admin_password is None or (
    settings.alerts.grafana_admin_password.get_secret_value() == ""
)
from base.native_process.os_platform import raise_fd_limit

# `_SESSION_SUFFIX` (PID + microsecond suffix, zero cross-process collision) and
# the tmpfs `_TEST_AVA_HOME` it names are defined in the env block at the top of
# this file — AVA_HOME has to be in the environment before the first project
# import, so the home cannot be created down here. Reused below for Redis channel
# isolation and machine_name, so concurrent sessions (dev agents + CI + manual
# runs) never share channels or home dirs.
assert settings.data_plane.events_channel == _TEST_EVENTS_CHANNEL
# A dev operator's .env may carry exec_timeout_seconds (≠ the field default),
# which the module-load settings reads. The timeout-marker tests
# (agent/graph/exec/tests/test_cancel.py) assert against the documented default, so pin it
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
from base.config.service_read import _all_domains_settings as _prewarm_full_settings

_prewarm_full_settings()


# Session default for the SDK's own agent id. `ava.self.*` / `ava.agents.*` read
# it to address `/api/agents/<AGENT_ID>/...`; leaving it None would 404 or crash
# earlier. It is only a placeholder for tests that never create an agent: serial
# ids are no longer reset between tests (see `_clean_state` — no RESTART IDENTITY),
# so the first spawn is NOT guaranteed to be id 1. Any test that exercises
# `ava.self.*` / `ava.agents.*` re-pins this to the id it actually created via
# `pin_agent(spawn_agent())` (`tests/fixtures/identity_restore.py`); the
# `identity_restore` plugin puts it back after the test. Do not rely on "the first spawn is 1" —
# capture the returned id.
from tests.fixtures.pin_agent import pin_agent

pin_agent(1)
# Remove AVA_AGENT_ID propagated from the agent process — any test that
# temporarily unbinds the context would re-derive one from this env var with
# owns_loop=False, corrupting subsequent tests.
os.environ.pop("AVA_AGENT_ID", None)

# `_TEST_AVA_HOME` is created and exported as AVA_HOME in the env block at the
# top of this file — the one source of the home; it is removed in
# `pytest_sessionfinish`.
# multi-machine setup: spawn_agent / claim_agent_row read machine_name(); the test home's
# `.env` (written in the block above) declares the session machine's name and its
# agent-runner flag, so Settings resolves both with nothing else to plant.
# machine capabilities default to agent-runner-only: gateway lifespan +
# post_agents preflight both read it, tests default to agent-runner path so that
# launch_agent_op locally launches the process; gateway-only tests monkeypatch
# override themselves. serve_agent_runner=true, serve_gateway left unset (=off).
# Pin both settings bools to the session default as well. Before the home was redirected
# ahead of the `settings` import, a module-loaded `settings` captured the host operator's
# AVA_MACHINE_SERVE_* out of their real ~/.ava/.env; with the redirect there is no host
# `.env` on the path to capture, so this is belt-and-braces — kept because the default a
# test inherits should be stated here rather than left to whatever the environment happens
# not to contain.
settings.general.machine_serve_agent_runner = True
settings.general.machine_serve_gateway = None

# machine_role defaults to agent-runner; gateway_api_base() reads gateway_url,
# which is pinned in the env block at the top of this file (it has to precede the
# Settings construction that `import ava` triggers). Tests never open a real TCP
# connection to it — `.invalid` is reserved by RFC 2606 and cannot resolve.

# ── Service ports: the session gets its own, never a prod default ──
#
# Redirecting AVA_HOME is not enough. Every service port falls back to a FIXED
# default when nothing overrides it — the fixed port table
# (base/host/env/port_table.py: gateway 8000, frontend 3000, every
# daemon's health port) — and those are the ports the operator's prod cluster on
# this same box is already bound to. So a test that starts a daemon binds prod's
# port, and a test that runs a healthcheck probes (or respawns!) prod's service.
#
# That is not hypothetical: on 2026-07-24 a pytest-leaked daemon took prod's
# health port and answered its /healthz for 98 minutes, so prod's watchdog saw
# green and never revived the real daemon — the whole cluster's `restarting`
# agents froze. `pg`/`redis` were already isolated this way (tests/_containers.py
# binds :0); these were the ports that were not.
#
# Every port a test binds or dials comes from the kernel or from the private range
# above 21000, and the whole table sits below it (base/cluster/tests/test_fixed_ports.py),
# so no test port can equal a table port. `base/cluster/tests/test_fixed_ports.py` also
# fails when a port-bearing setting in this session still holds its table value.
#
# Pinned in the settings singleton (in-process readers, which have already
# constructed Settings by now) AND in os.environ (any subprocess a test spawns).
# Kernel-assigned, so concurrent xdist workers — one import of this module each —
# never collide either.


def distinct_free_ports(count: int) -> list[int]:
    """`count` different free localhost ports: every socket stays bound until all
    are chosen, so the kernel cannot hand the same number out twice. The small race
    before a server binds one is the same that tests/_containers.py accepts for
    pg/redis."""
    with contextlib.ExitStack() as stack:
        socks: list[socket.socket] = []
        for _ in range(count):
            sock = stack.enter_context(
                contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            )
            sock.bind(("127.0.0.1", 0))
            socks.append(sock)
        return [int(sock.getsockname()[1]) for sock in socks]


def _pin_setting(field: str, value: object) -> None:
    """Force a settings field for this session, in the singleton and in the env
    under pydantic's `AVA_<FIELD>` name (subprocesses read the latter)."""
    set_field(field, value)
    os.environ[f"AVA_{field.upper()}"] = str(value)


# Every daemon that registers a health port — read off the same map the endpoint table
# consults, so a newly registered daemon is isolated the moment it is added there
# rather than silently inheriting its prod default.
#
# One allocation hands out every port below, so no two of them (and none of the
# session's table slots) can be the same number: separate `bind(0)` calls release
# each port at once and the kernel may return it again.
_HEALTH_PORT_FIELDS = tuple(_HEALTH_PORT_OVERRIDES.values())
_ports = iter(distinct_free_ports(len(_HEALTH_PORT_FIELDS) + 7 + len(FIXED_PORTS)))
for _health_port_field in _HEALTH_PORT_FIELDS:
    _pin_setting(_health_port_field, next(_ports))

_pin_setting("gateway_health_url", f"http://127.0.0.1:{next(_ports)}/api/health")
_pin_setting("frontend_healthcheck_url", f"http://127.0.0.1:{next(_ports)}")
# The rest of the settings that default to a table port. The permissions helper
# port is pinned in the singleton only: its env key is popped above on purpose,
# and a set key would read as a helper spawn context in every child.
_pin_setting("gateway_port", next(_ports))
_pin_setting("browser_cdp_port", next(_ports))
_pin_setting("grafana_port", next(_ports))
_pin_setting("memory_search_port", next(_ports))
_pin_setting("memory_search_uri", f"http://127.0.0.1:{settings.services.memory_search_port}")
set_field("permissions_helper_port", next(_ports))

# The ports every test home born in this session records (`guards.py`): one
# kernel-assigned port per slot of the fixed table.
_SESSION_PORTS: dict[str, int] = dict(zip(FIXED_PORTS, _ports, strict=True))


# Belt-and-suspenders on the OS-jobs switch already in `os.environ` at the top of
# this file: an operator's real `~/.ava/.env` is loaded by `base.host.env.dotenv_boot`
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
# fixture layered on later. `tests/harness/test_home_isolation.py` compares against it to
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
# sanctioned divergence, and it is sanctioned because those fixtures live in a plugin
# loaded from the repo-root conftest.py, where session scope and the blast radius are
# the same thing.
_PRISTINE_ENV: dict[str, str] = dict(os.environ)


@pytest.fixture(scope="session")
def pristine_env() -> Mapping[str, str]:
    """The environment as of the end of this file's env block, before any fixture ran.

    A fixture rather than an importable constant so the value travels through
    pytest's own channel — this module's import name depends on how the plugin is
    loaded, and a test that reached in by module path would be coupled to it.
    """
    return _PRISTINE_ENV


@pytest.fixture(scope="session")
def session_ports() -> dict[str, int]:
    """One kernel-assigned port per slot of the fixed table, for test homes.

    A fixture for the same reason as `pristine_env`: it travels through pytest's
    own channel instead of a module path that depends on how the plugin loads.
    """
    return dict(_SESSION_PORTS)
