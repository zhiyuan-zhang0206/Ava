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
os.environ.setdefault("AVA_CONFIG_FETCH", "skip")


def _fields() -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """The raw flat field registry (name -> _FieldRef with scope/capability)."""
    from shared.config import _FIELDS, _schema_extra, field_alias

    return {  # pyright: ignore[reportUnknownVariableType]
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
    out = set()  # pyright: ignore[reportUnknownVariableType]
    for _name, meta in _fields().items():  # pyright: ignore[reportUnknownVariableType]
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
                "AVA_DB_URL",
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

    def test_identity_keys_are_the_machine_identity_fields(self) -> None:
        from shared.env_registry import env_identity_keys

        expected = _aliases_with(scope=("host",)) & {
            "AVA_MACHINE_SERVE_GATEWAY",
            "AVA_MACHINE_SERVE_AGENT_RUNNER",
            "AVA_MACHINE_HOST",
            "AVA_MACHINE_NAME",
            "AVA_MACHINE_DESCRIPTION",
            "AVA_GATEWAY_URL",
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
            "AVA_BROWSER_CDP_PORT",
            "AVA_PERMISSIONS_HELPER_PORT",
            "AVA_DB_URL",
            "AVA_REDIS_URL",
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
            "XAI_API_KEY",
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
        }
        meta = _fields()  # pyright: ignore[reportUnknownVariableType]
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
            "AVA_PRIMARY_GATEWAY_URL",
            "PATH",
            "VIRTUAL_ENV",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
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
