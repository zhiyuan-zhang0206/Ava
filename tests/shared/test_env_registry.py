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

import pytest

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
        from shared.env_registry import MANIFEST_CERTIFICATION_SECRET_ENV, session_forward_keys

        expected = _aliases_with(scope=("host",)) - {MANIFEST_CERTIFICATION_SECRET_ENV}
        assert session_forward_keys() == expected
        # The F-s3-4 headline: per-agent identity never rides a daemon session.
        assert "AVA_AGENT_ID" not in session_forward_keys()

    def test_manifest_certification_proof_is_not_forwarded_to_model_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host finalizer receives its proof only through a dedicated projection."""
        from shared import dotenv_boot
        from shared.env_registry import (
            MANIFEST_CERTIFICATION_FINALIZER_ENV,
            MANIFEST_CERTIFICATION_SECRET_ENV,
            child_env,
            manifest_certification_secret_env,
        )

        monkeypatch.setattr(
            dotenv_boot,
            "manifest_certification_secret_from_env_file",
            lambda: "host-finalizer-proof",
        )
        assert manifest_certification_secret_env() == {
            MANIFEST_CERTIFICATION_SECRET_ENV: "host-finalizer-proof",
            MANIFEST_CERTIFICATION_FINALIZER_ENV: "1",
        }
        for role in ("gateway", "runner", "agent"):
            assert MANIFEST_CERTIFICATION_SECRET_ENV not in child_env(role, "posix")
            assert MANIFEST_CERTIFICATION_FINALIZER_ENV not in child_env(role, "posix")

    def test_finalizer_boot_ticket_retains_proof_from_its_unit_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ticket is consumed before the finalizer loads its unit `.env`."""
        from shared import dotenv_boot
        from shared.env_registry import (
            MANIFEST_CERTIFICATION_FINALIZER_ENV,
            MANIFEST_CERTIFICATION_SECRET_ENV,
        )

        proof = "host-finalizer-proof"
        env_file = tmp_path / ".env"
        env_file.write_text(f"{MANIFEST_CERTIFICATION_SECRET_ENV}={proof}\n")
        monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
        monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", tmp_path / "mirror.env")
        monkeypatch.setattr(dotenv_boot, "_enforce_cluster_env_authority", lambda: None)
        monkeypatch.setattr(dotenv_boot, "_manifest_finalizer_boot_authorized", False)
        monkeypatch.delenv(MANIFEST_CERTIFICATION_SECRET_ENV, raising=False)
        monkeypatch.setenv(MANIFEST_CERTIFICATION_FINALIZER_ENV, "1")

        dotenv_boot.load_ava_env()

        assert os.environ[MANIFEST_CERTIFICATION_SECRET_ENV] == proof
        assert MANIFEST_CERTIFICATION_FINALIZER_ENV not in os.environ

    def test_session_forward_carries_the_ambient_passthroughs(self) -> None:
        from shared.env_registry import HOST_PASSTHROUGH_KEYS

        assert frozenset({"DISPLAY", "WAYLAND_DISPLAY", "HOME"}) == HOST_PASSTHROUGH_KEYS

    def test_child_env_carries_the_machine_network_proxy_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2095: the machine's proxy configuration is ambient host config —
        the egress a building child (`npm run build` fetching Google Fonts for
        next/font) cannot re-source itself — so every role's child env carries it,
        in both spellings (a shell exports HTTP(S)_PROXY; npm/node/curl read the
        lowercase set), non-empty only (an empty ALL_PROXY means "direct")."""
        from shared.env_registry import NETWORK_PROXY_KEYS, child_env, network_proxy_configured

        assert {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        } == NETWORK_PROXY_KEYS
        for key in NETWORK_PROXY_KEYS:
            monkeypatch.delenv(key, raising=False)
        assert network_proxy_configured() is False
        for role in ("gateway", "runner", "agent"):
            assert not (NETWORK_PROXY_KEYS & set(child_env(role, "posix")))
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
        monkeypatch.setenv("ALL_PROXY", "")
        # The family's single reader (the Feishu ws handshake asks it): any
        # non-empty proxy key, either spelling.
        assert network_proxy_configured() is True
        for role in ("gateway", "runner", "agent"):
            for platform in ("posix", "windows"):
                env = child_env(role, platform)
                assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
                assert env["no_proxy"] == "localhost,127.0.0.1"
                assert "ALL_PROXY" not in env

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
        # machine identity.
        expected |= {"AVA_MEMORY_REMOTE"}
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
            "AVA_REDIS_ADMIN_PASSWORD",
            "AVA_REDIS_PASSWORD",
            "AVA_EVENTS_CHANNEL",
        } | set(health_port_env_aliases().values())
        assert derived_env_keys() == expected

    def test_health_port_aliases_are_host_scope_settings_fields(self) -> None:
        from shared.env_registry import health_port_env_aliases

        aliases = health_port_env_aliases()
        assert set(aliases) == {
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


def _block_env(base: int) -> dict[str, str]:
    """The full health-port env a block-style unit at `base` carries."""
    from shared.env_registry import health_port_env

    return {alias: str(int(port)) for alias, port in health_port_env(base).items()}
