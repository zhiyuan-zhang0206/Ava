"""Unit tests for the bootstrap config allowlist (Plan B2)."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from shared import config
from shared.envfile import upsert_env


def _write_runner_password(home: Path) -> None:
    upsert_env(
        home / ".env",
        {
            "AVA_DB_URL": str(config.settings.data_plane.db_url),
            "AVA_RUNNER_DB_PASSWORD": "runner-password",
        },
    )


def test_bootstrap_values_use_env_aliases_and_skip_unset() -> None:
    vals = config.bootstrap_config_values()
    # DB/Redis URLs are required (always set in test env) → present, keyed by alias.
    assert "AVA_DB_URL" in vals
    assert "AVA_REDIS_URL" in vals
    # Every bootstrap projection is the runner identity; only the host may be
    # rewritten for a remote runner.
    expected = str(config.settings.data_plane.db_url)
    reachable = config._self_machine_host()
    if not config.is_loopback_host(reachable):
        expected = config.url_with_host(expected, reachable)
    served, owner = urlsplit(vals["AVA_DB_URL"]), urlsplit(expected)
    assert served.username == "ava_runner"
    assert served.hostname == owner.hostname
    assert served.port == owner.port
    assert served.path == owner.path
    # values are strings
    assert all(isinstance(v, str) for v in vals.values())


def test_bootstrap_excludes_machine_local_fields() -> None:
    # Machine-local / bootstrap / per-host fields must never be relayed.
    vals = config.bootstrap_config_values()
    for excluded in (
        "AVA_MACHINE_NAME",
        "AVA_GATEWAY_URL",
        "AVA_GATEWAY_PIDFILE",
    ):
        assert excluded not in vals


def test_bootstrap_serves_explicit_set_to_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A field explicitly set to empty on the gateway (e.g.
    AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT="" exported by the bench
    entrypoint) is served as ALIAS="" rather than dropped. Dropping it would
    silently revert the recipient to the field default — a bench agent then
    got the default skill index back and crashed on the disabled ava.skills
    (issue #948)."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)  # no .env overrides
    _write_runner_password(tmp_path)
    monkeypatch.setattr(config.settings.agent, "skills_to_inject_into_system_prompt", [])

    vals = config.bootstrap_config_values()
    assert vals["AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT"] == ""


def test_bootstrap_still_skips_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """None (no value at all) stays unserved — env text can't express it; the
    recipient falls back to the field default. Distinct from set-to-empty."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    _write_runner_password(tmp_path)
    monkeypatch.setattr(config.settings.observability, "trace_tags", None)

    vals = config.bootstrap_config_values()
    assert "AVA_TRACE_TAGS" not in vals


def test_bootstrap_fields_are_valid_field_names() -> None:
    # every allowlisted field name must exist on Settings
    for name in config.BOOTSTRAP_FIELDS:
        assert name in config.FIELD_INFOS, name


def test_bootstrap_includes_all_required_fields() -> None:
    # A gateway-sourced agent-runner builds Settings from ONLY this bundle (+ the
    # tiny bootstrap env). Every required no-default field's alias must be in the
    # bundle, else Settings() raises ValidationError and the daemon cannot boot.
    from pydantic_core import PydanticUndefined

    def alias_of(name: str) -> str:
        f = config.FIELD_INFOS[name]
        return f.serialization_alias or f.alias or name.upper()

    required_aliases = {
        alias_of(name)
        for name, f in config.FIELD_INFOS.items()
        if f.default is PydanticUndefined and f.default_factory is None
    }
    bundle = set(config.bootstrap_config_values())
    missing = required_aliases - bundle
    assert not missing, f"bundle omits required Settings fields: {sorted(missing)}"


def test_bootstrap_serves_no_daemon_health_port() -> None:
    """No `AVA_*_HEALTH_PORT` travels from the gateway to a runner.

    A port block is a property of the CLUSTER while the collision domain is one
    MACHINE's localhost namespace; serving these made two agent-runners of the
    same cluster on one machine take the same ports by construction (issue #977).
    Nothing here is cluster-constrained — a runner computes its own ops URL from
    its own `health_port('ops')` and registers it, and the gateway reads that URL
    back off the machines row.

    Asserted against the live service->env-var map, and on BOTH the served
    payload and the field allowlist: `bootstrap_config_values` skips a None, so a
    field that regained cluster scope would pass a payload-only check on any
    gateway that had not set it."""
    from shared.env_registry import health_port_env_aliases

    health_aliases = set(health_port_env_aliases().values())
    assert not (health_aliases & set(config.bootstrap_config_values()))
    assert not (health_aliases & {config.field_alias(n) for n in config.BOOTSTRAP_FIELDS})


# ── data-plane URLs are served with the gateway's reachable host, not loopback ──
#
# A remote agent-runner materializes the payload and dials the URLs it got; a
# loopback host would make it dial ITSELF. The gateway's own .env keeps
# 127.0.0.1 (it dials itself over loopback), so the swap happens at serve time
# when the gateway has a non-loopback reachable host (AVA_MACHINE_HOST).


def test_bootstrap_serves_reachable_host_for_loopback_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """db/redis URLs served to a remote runner name the gateway's reachable
    address (host swapped, identity/port/db preserved verbatim)."""
    from urllib.parse import urlsplit

    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)  # no .env overrides
    _write_runner_password(tmp_path)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "10.0.0.3")

    db = str(config.settings.data_plane.db_url)
    redis = str(config.settings.data_plane.redis_url)
    vals = config.bootstrap_config_values()
    served_db, served_redis = urlsplit(vals["AVA_DB_URL"]), urlsplit(vals["AVA_REDIS_URL"])
    # host swapped to the reachable address; port + database kept verbatim
    assert served_db.hostname == "10.0.0.3"
    assert served_redis.hostname == "10.0.0.3"
    assert served_db.port == urlsplit(db).port
    assert served_db.path == urlsplit(db).path
    assert served_redis.port == urlsplit(redis).port
    assert served_redis.path == urlsplit(redis).path


def test_bootstrap_keeps_loopback_when_gateway_is_single_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway whose own reachable host is loopback (single box) serves the
    URLs as-is — there is no remote runner to reach it, and swapping to
    `localhost` would be a no-op."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    _write_runner_password(tmp_path)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "localhost")

    vals = config.bootstrap_config_values()
    served, owner = urlsplit(vals["AVA_DB_URL"]), urlsplit(config.settings.data_plane.db_url)
    assert served.username == "ava_runner"
    assert served.hostname == owner.hostname
    assert served.port == owner.port
    assert served.path == owner.path


def test_bootstrap_keeps_existing_reachable_url_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A .env URL that already names the reachable host (prod's historical
    hand-set URLs) passes through verbatim — no rewrite of a non-loopback host."""
    from shared import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    _write_runner_password(tmp_path)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "10.0.0.3")
    dp = config.settings.data_plane
    # parts-built, scanner-safe (same convention as tests/cli/test_converge.py)
    host_url = f"postgresql://ava_main:{'sek'}@10.0.0.2:5433/ava_main"
    monkeypatch.setattr(dp, "db_url", host_url)
    upsert_env(tmp_path / ".env", {"AVA_DB_URL": host_url})

    vals = config.bootstrap_config_values()
    served, owner = urlsplit(vals["AVA_DB_URL"]), urlsplit(host_url)
    assert served.username == "ava_runner"
    assert served.hostname == owner.hostname
    assert served.port == owner.port
    assert served.path == owner.path
