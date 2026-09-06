"""Derivation-rule tests for the env registry (R2 convergence point A).

The old seam tests (test_profile_env_keys.py) held hand-written snapshots
against the field registry. The snapshots are gone — the registry's projections
(`shared/env_registry.py`) are pure functions of the Settings class metadata.
These tests pin the DERIVATION RULES instead: each projection is re-computed
here independently from the raw metadata (`_FIELDS` + scope/capability/alias),
so a future change to a rule (e.g. reverting to capability-derived sets — the
#1570 P0 shape) is a deliberate, test-breaking change. Structural invariants
(A1/A3) are pinned alongside.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Ensure settings-lite so we can import config without a real .env
os.environ["AVA_CONFIG_FETCH"] = (
    "skip"  # assignment, not setdefault: a setdefault would silently keep an inherited value (the login-shell .env leak class) instead of pinning settings-lite
)


def _fields() -> dict:
    """The raw flat field registry (name -> _FieldRef with scope/capability)."""
    from shared.config import _FIELDS, _schema_extra, field_alias

    return {
        name: {
            "alias": field_alias(name),
            "scope": _schema_extra(ref.info).get("scope"),
            "capability": ref.capability,
        }
        for name, ref in _FIELDS.items()
    }


def _aliases_with(
    *, scope: tuple[str, ...] | None = None, capability: str | None = None
) -> frozenset[str]:
    """Independent recomputation of a scope/capability slice of the registry."""
    out = set()
    for _name, meta in _fields().items():
        if scope is not None and meta["scope"] not in scope:
            continue
        if capability is not None and meta["capability"] != capability:
            continue
        out.add(meta["alias"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return frozenset(out)  # pyright: ignore[reportUnknownArgumentType]


class TestScopeDerivationRules:
    """The scope-derived projections must equal an independent registry scan —
    a new field with the right scope metadata lands in every correct projection
    (A3); a rule change (e.g. deriving from capability — the #1570 P0 shape) is
    a deliberate, test-breaking change."""

    def test_cluster_scope_is_cluster_pinned_plus_cluster_default(self) -> None:
        from shared.env_registry import cluster_scope_aliases

        expected = _aliases_with(scope=("cluster-pinned", "cluster-default"))
        assert cluster_scope_aliases() == expected
        assert len(expected) > 150  # the six-gap class lives in this set

    def test_session_forward_is_host_scope(self) -> None:
        from shared.env_registry import session_forward_keys

        expected = _aliases_with(scope=("host",))
        assert session_forward_keys() == expected
        # The F-s3-4 headline: per-agent identity never rides a daemon session.
        assert "AVA_AGENT_ID" not in session_forward_keys()

    def test_session_forward_carries_the_ambient_passthroughs(self) -> None:
        from shared.env_registry import HOST_PASSTHROUGH_KEYS

        assert frozenset({"DISPLAY", "WAYLAND_DISPLAY", "HOME"}) == HOST_PASSTHROUGH_KEYS

    def test_agent_forward_is_session_plus_agent_scope_plus_guide(self) -> None:
        from shared.env_registry import agent_forward_keys, session_forward_keys

        expected = (
            session_forward_keys()
            | _aliases_with(scope=("agent",))
            | {
                "AVA_HOME",
                "AVA_CLUSTER_SECRET",
                "AVA_GATEWAY_URL",
                "AVA_GATEWAY_PORT",
                "SSL_CERT_FILE",
                "REQUESTS_CA_BUNDLE",
                "AVA_AGENT_CONFIG_OVERLAY",
                "AVA_AGENT_BIRTH_CONFIG",
            }
        )
        assert agent_forward_keys() == expected
        # An agent child is a single agent — its identity is set by the launcher
        # (ops/agent_launch.py), never inherited from the parent env.
        assert "AVA_AGENT_ID" not in agent_forward_keys()

    def test_agent_runner_cluster_aliases_are_capability_plus_cluster_scope(self) -> None:
        from shared.env_registry import agent_runner_cluster_aliases

        expected = _aliases_with(
            capability="agent-runner", scope=("cluster-pinned", "cluster-default")
        )
        assert agent_runner_cluster_aliases() == expected
        # Disjoint from the host-scope session view (host-scope agent-runner
        # keys are deliberately kept on the gateway — single-box daemons).
        from shared.env_registry import session_forward_keys

        assert not (agent_runner_cluster_aliases() & session_forward_keys())


class TestConsumptionMatrixDeclarations:
    """The explicit consumption-matrix rows (identity / derived / seed / health
    ports) stay exactly the declared facts — declared by FIELD NAME so an alias
    rename follows automatically."""

    def test_identity_keys_are_the_home_owned_identity_and_tool_fields(self) -> None:
        from shared.env_registry import env_identity_keys

        expected = _aliases_with(scope=("host",)) & {
            "AVA_MACHINE_SERVE_GATEWAY",
            "AVA_MACHINE_SERVE_AGENT_RUNNER",
            "AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
            "AVA_MACHINE_HOST",
            "AVA_MACHINE_NAME",
            "AVA_MACHINE_DESCRIPTION",
            "AVA_GATEWAY_URL",
            "AVA_REDIS_BIN_DIR",
        }
        # memory_remote is cluster-pinned (a remote is cluster config) but still
        # machine identity; AVA_PRIMARY_GATEWAY_URL is the deprecated alias row.
        expected |= {"AVA_MEMORY_REMOTE", "AVA_PRIMARY_GATEWAY_URL"}
        assert env_identity_keys() == expected
        assert "AVA_HOME" not in env_identity_keys()

    def test_derived_keys_are_the_derive_env_surface(self) -> None:
        from shared.env_registry import derived_env_keys, health_port_env_aliases

        expected = {
            "AVA_CLUSTER_SECRET",
            "AVA_GATEWAY_PORT",
            "AVA_GATEWAY_URL",
            "AVA_GATEWAY_HEALTH_URL",
            "AVA_FRONTEND_HEALTHCHECK_URL",
            "AVA_APP_PORT",
            "AVA_MILVUS_PORT",
            "AVA_MILVUS_URI",
            "AVA_MEMORY_SEARCH_PORT",
            "AVA_MEMORY_SEARCH_URI",
            "AVA_BROWSER_CDP_PORT",
            "AVA_PERMISSIONS_HELPER_PORT",
            "AVA_DB_URL",
            "AVA_REDIS_URL",
            "AVA_DB_ADMIN_PASSWORD",
            "AVA_REDIS_ADMIN_PASSWORD",
            "AVA_REDIS_PASSWORD",
            "AVA_EVENTS_CHANNEL",
        } | set(health_port_env_aliases().values())
        assert derived_env_keys() == expected

    def test_seed_allowlist_is_the_provider_keys(self) -> None:
        """Pinned as an exact set, so widening it is a decision someone makes on
        purpose. AVA_DASHSCOPE_BASE_URL is the one member that is not a
        credential: a dedicated Model Studio workspace mints its key for its own
        host, so seeding DASHSCOPE_API_KEY without it hands a fresh worktree a
        key it cannot spend."""
        from shared.env_registry import seed_allowlist

        expected = {
            "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "MIMO_API_KEY",
            "MOONSHOT_API_KEY",
            "GLM_API_KEY",
            "DASHSCOPE_API_KEY",
            "AVA_DASHSCOPE_BASE_URL",
            "BRAVE_API_KEY",
            "JINA_API_KEY",
        }
        assert seed_allowlist() == expected
        # A seeded worktree must never inherit prod's identity or its secret.
        from shared.env_registry import derived_env_keys, env_identity_keys

        assert not (seed_allowlist() & (derived_env_keys() | env_identity_keys()))
        assert "AVA_TELEGRAM_BOT_TOKEN" not in seed_allowlist()

    def test_health_port_aliases_are_host_scope_settings_fields(self) -> None:
        from shared.env_registry import health_port_env_aliases

        aliases = health_port_env_aliases()
        assert set(aliases) == {
            "restarter",
            "labeler",
            "heartbeat",
            "task_maintenance",
            "events_maintenance",
            "pg_backup",
            "memory_indexer",
            "ops",
            "delivery_watchdog",
            "im_bridge",
            "page_server",
            # The hosted agent-runner (future/infra/agent-runner-as-server.md).
            # Listed even though the service only starts under AVA_RUNNER_MODE
            # hosted: the alias must exist for every declared health-port
            # service, gated or not, or the runner projection has no key to
            # carry when a cluster does flip the mode.
            "agent_host",
            "pitr_uploader",
            "pitr_base_backup",
            "gateway_watchdog",
            "agent_runner_watchdog",
        }
        meta = _fields()
        for svc, alias in aliases.items():
            assert meta[f"{svc}_health_port"]["alias"] == alias
            assert meta[f"{svc}_health_port"]["scope"] == "host"


class TestRegistryInvariants:
    """A1/A2: every key the projections touch is registered exactly once, and
    the projections are pure functions of the registry (no hand-written set)."""

    def test_passthrough_rows_never_collide_with_settings_aliases(self) -> None:
        """A1: a key declared as both a Settings alias and a passthrough row is
        the duplicate-declaration drift class — the registry refuses it at the
        first projection call."""
        from shared.env_registry import child_env

        # Exercises _ensure_validated(); raises RuntimeError on a collision.
        child_env("agent", "posix")

    def test_every_projection_key_is_registered(self) -> None:
        """A1: no orphan keys — every alias a projection emits is either a
        Settings field alias or a declared passthrough row."""
        import shared.env_registry as er

        registered = set(_aliases_with(scope=())) | _all_aliases()
        registered |= er.HOST_PASSTHROUGH_KEYS | er.WINDOWS_SYSTEM_ENV_KEYS
        registered |= {
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "AVA_AGENT_CONFIG_OVERLAY",
            "AVA_AGENT_BIRTH_CONFIG",
            "AVA_REDIS_PASSWORD",
            "AVA_PRIMARY_GATEWAY_URL",
            "PATH",
            "VIRTUAL_ENV",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
        registered |= er._enabled_provider_key_envs()
        for proj in (
            er.cluster_scope_aliases(),
            er.agent_runner_cluster_aliases(),
            er.env_identity_keys(),
            er.derived_env_keys(),
            er.seed_allowlist(),
            er.session_forward_keys(),
            er.agent_forward_keys(),
        ):
            assert proj <= registered, f"projection carries unregistered keys: {proj - registered}"

    def test_windows_system_keys_are_declared_once(self) -> None:
        """The old parallel copy in shared/session_env.py is gone — a single
        declaration in the registry; USERNAME/USERDOMAIN are the Task #963
        lock (getpass.getuser() on Windows)."""
        from shared.env_registry import WINDOWS_SYSTEM_ENV_KEYS, child_env

        assert "USERNAME" in WINDOWS_SYSTEM_ENV_KEYS
        assert "USERDOMAIN" in WINDOWS_SYSTEM_ENV_KEYS
        os.environ["SYSTEMROOT"] = r"C:\Windows"
        try:
            env = child_env("agent", "windows")
            assert env["SYSTEMROOT"] == r"C:\Windows"
            assert "SYSTEMROOT" not in child_env("agent", "posix")
        finally:
            os.environ.pop("SYSTEMROOT", None)

    def test_windows_child_env_sets_utf8_mode(self) -> None:
        """Task #2540: the Windows positive allowlist wholesale-replaces the
        child env, dropping ensure_utf8_stdio's PYTHONUTF8 seed — a daemon
        (agent-host) and its in-process hosted agents then start on the legacy
        code page and crash printing CJK (win agent 2528). The windows branch
        must inject PYTHONUTF8=1 for every role; POSIX children are unchanged
        (locale UTF-8)."""
        from shared.env_registry import child_env

        for role in ("agent", "runner", "gateway"):
            assert child_env(role, "windows")["PYTHONUTF8"] == "1"
            assert "PYTHONUTF8" not in child_env(role, "posix")


def _all_aliases() -> frozenset[str]:
    return _aliases_with(scope=("cluster-pinned", "cluster-default", "host", "agent"))


def test_env_registry_imports_on_clean_env_without_config_package() -> None:
    """Task #1099 regression: importing env_registry first (the install.sh
    --worktree boot path) must not circular-import through the config package.

    Before the fix, `shared.config_registry` built `FIELD_INFOS` at module
    level, which ran `_build_registry()` during the module import; its deferred
    `shared.config` package import re-entered the half-initialized registry
    module (the package __init__ re-imports `field_alias` from it) and raised
    ``ImportError: cannot import name 'field_alias' from partially initialized
    module``. A fresh subprocess with no pre-imported `shared.config`
    reproduces the exact install.sh --worktree boot order.
    """
    result = subprocess.run(  # fixed argv, repo code, no shell
        [
            sys.executable,
            "-c",
            "import shared.env_registry; import shared.config; print('ok')",
        ],
        cwd=Path(__file__).resolve().parents[2],  # repo root
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _backfill(existing: dict[str, str]) -> dict[str, str]:
    from shared.env_registry import backfill_missing_health_ports

    return backfill_missing_health_ports(existing)


def _block_env(base: int) -> dict[str, str]:
    """The full health-port env a block-style unit at `base` carries."""
    from shared.env_registry import health_port_env

    return {alias: str(int(port)) for alias, port in health_port_env(base).items()}


def test_backfill_derives_missing_keys_from_a_block_unit() -> None:
    """win-shaped .env: the present keys prove one base (18114) but the slots
    added after enroll (agent_host, the capability watchdogs) are absent — the
    healer derives them at base + offset, and never rewrites a present key."""
    from shared.port_block import PORT_OFFSETS

    base = 18114
    full = _block_env(base)
    present = {
        "AVA_RESTARTER_HEALTH_PORT",
        "AVA_LABELER_HEALTH_PORT",
        "AVA_HEARTBEAT_HEALTH_PORT",
        "AVA_TASK_MAINTENANCE_HEALTH_PORT",
        "AVA_MEMORY_INDEXER_HEALTH_PORT",
        "AVA_OPS_HEALTH_PORT",
        "AVA_EVENTS_MAINTENANCE_HEALTH_PORT",
    }
    keys = {alias: full[alias] for alias in present}
    missing = _backfill(keys)
    assert missing["AVA_AGENT_HOST_HEALTH_PORT"] == str(base + PORT_OFFSETS["agent_host"])
    assert missing["AVA_GATEWAY_WATCHDOG_HEALTH_PORT"] == str(
        base + PORT_OFFSETS["gateway_watchdog"]
    )
    assert "AVA_OPS_HEALTH_PORT" not in missing  # present keys are never rewritten
    assert missing == {alias: port for alias, port in full.items() if alias not in present}


def test_backfill_refuses_a_legacy_pin_sequence() -> None:
    """prod ~/.ava's fixed 8102-8111 pins must not be read as a block: some line
    up with PORT_OFFSETS by accident (restarter/labeler/memory_indexer/ops all
    "solve" to 8099), the legacy slot order of the rest does not."""
    legacy = {
        "AVA_RESTARTER_HEALTH_PORT": "8102",
        "AVA_LABELER_HEALTH_PORT": "8103",
        "AVA_MEMORY_INDEXER_HEALTH_PORT": "8105",
        "AVA_OPS_HEALTH_PORT": "8106",
        "AVA_HEARTBEAT_HEALTH_PORT": "8107",  # legacy slot order, not offset order
        "AVA_TASK_MAINTENANCE_HEALTH_PORT": "8108",
    }
    assert _backfill(legacy) == {}


def test_backfill_needs_two_agreeing_keys_on_a_block_floor_base() -> None:
    assert _backfill({"AVA_OPS_HEALTH_PORT": "8113"}) == {}  # one key proves nothing
    # two keys that disagree
    assert _backfill({"AVA_OPS_HEALTH_PORT": "18121", "AVA_LABELER_HEALTH_PORT": "9999"}) == {}
    # two keys agreeing below the block floor (8106): a legacy band, not a block
    assert _backfill({"AVA_RESTARTER_HEALTH_PORT": "8109", "AVA_LABELER_HEALTH_PORT": "8110"}) == {}


def test_backfill_ignores_a_present_but_misplaced_key() -> None:
    """win's 2026-09-02 emergency pin put agent_host ON the block base (18114)
    instead of base+19 — the healer heals ABSENCE, never drift; a wrong-slot
    hand-set port is the operator's to move through the config surface."""
    from shared.port_block import PORT_OFFSETS

    base = 18114
    keys = {
        "AVA_OPS_HEALTH_PORT": str(base + PORT_OFFSETS["ops"]),
        "AVA_AGENT_HOST_HEALTH_PORT": str(base),  # wrong slot: base, not base+19
    }
    assert _backfill(keys) == {}


def test_backfill_majority_base_ignores_a_single_outlier() -> None:
    """wsl-shaped .env (2026-09-02): all keys prove one block base except one
    drifted hand-set key — the agreeing majority must still prove the block,
    and the outlier must not abort the whole backfill."""
    from shared.port_block import PORT_OFFSETS

    base = 20027
    full = _block_env(base)
    present = set(full) - {"AVA_AGENT_HOST_HEALTH_PORT", "AVA_GATEWAY_WATCHDOG_HEALTH_PORT"}
    keys = {alias: full[alias] for alias in present}
    keys["AVA_AGENT_RUNNER_WATCHDOG_HEALTH_PORT"] = "20024"  # the drifted outlier
    missing = _backfill(keys)
    assert missing["AVA_AGENT_HOST_HEALTH_PORT"] == str(base + PORT_OFFSETS["agent_host"])
    assert missing["AVA_GATEWAY_WATCHDOG_HEALTH_PORT"] == str(
        base + PORT_OFFSETS["gateway_watchdog"]
    )


def test_backfill_refuses_an_ambiguous_tie_between_two_bases() -> None:
    """Two keys on base A and two on base B prove neither — guessing would bind
    ports nobody asked for."""
    from shared.port_block import PORT_OFFSETS

    base, other = 18114, 20027
    keys = {
        "AVA_OPS_HEALTH_PORT": str(base + PORT_OFFSETS["ops"]),
        "AVA_LABELER_HEALTH_PORT": str(base + PORT_OFFSETS["labeler"]),
        "AVA_HEARTBEAT_HEALTH_PORT": str(other + PORT_OFFSETS["heartbeat"]),
        "AVA_RESTARTER_HEALTH_PORT": str(other + PORT_OFFSETS["restarter"]),
    }
    assert _backfill(keys) == {}


def test_backfill_unparseable_values_are_outliers_not_fatal() -> None:
    """A corrupt value must not abort the backfill when the rest of the block
    still agrees (previously any TypeError returned {})."""
    from shared.port_block import PORT_OFFSETS

    base = 18114
    full = _block_env(base)
    present = set(full) - {"AVA_MEMORY_INDEXER_HEALTH_PORT"}
    keys = {alias: full[alias] for alias in present}
    keys["AVA_OPS_HEALTH_PORT"] = "not-a-port"
    missing = _backfill(keys)
    assert missing["AVA_MEMORY_INDEXER_HEALTH_PORT"] == str(base + PORT_OFFSETS["memory_indexer"])
    assert "AVA_OPS_HEALTH_PORT" not in missing
