"""Swappable connection layer (Task #1752): the URL-switched remote predicate,
the TLS fallback rule, and the config-driven pool sizing.

The data plane is the switch: `DataPlaneSettings.is_remote` is derived from the
URL dial hosts (after the self-dial loopback rewrite), so "change the URL =
switch local ↔ remote/SaaS" holds with no extra flag. `_sslmode_for_url` keeps
the URL as the primary TLS source (a SaaS connection string's own sslmode
wins) and makes AVA_DB_SSLMODE the fallback default. `_resolved_pool_size`
makes the pool footprint config-driven with explicit caller sizes winning.
"""

from __future__ import annotations

from shared.config.data_plane import DataPlaneSettings, resolved_pool_size, sslmode_for_url
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL

# A foreign host that could never be this machine's own reachable address in
# the test env (AVA_MACHINE_HOST=localhost, so a `localhost` URL is rewritten
# to loopback and reads local).
_FOREIGN_DB = "postgresql://ava:pw@10.9.8.7:5432/ava"
_FOREIGN_REDIS = "rediss://ava:pw@10.9.8.7:6380/0"
_LOOPBACK_DB = "postgresql://ava:pw@127.0.0.1:5432/ava"
_LOOPBACK_REDIS = "redis://ava:pw@127.0.0.1:6380/0"
_LOCAL_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture, not a real credential


def _settings(db_url: str, redis_url: str) -> DataPlaneSettings:
    return DataPlaneSettings(db_url=db_url, redis_url=redis_url)  # pyright: ignore[reportCallIssue]


# ─── the remote predicate: URL-derived, no extra flag ────────────────────────


def test_is_remote_false_for_loopback_urls() -> None:
    assert _settings(_LOOPBACK_DB, _LOOPBACK_REDIS).is_remote is False


def test_is_remote_false_for_self_named_host() -> None:
    """A URL naming this machine's own reachable address is loopback-rewritten
    by DataPlaneSettings, so the predicate must read it as local (the
    single-box case where the reachable address is a name)."""
    assert _settings("postgresql://ava:pw@localhost:5432/ava", _LOOPBACK_REDIS).is_remote is False


def test_is_remote_true_for_foreign_hosts() -> None:
    assert _settings(_FOREIGN_DB, _FOREIGN_REDIS).is_remote is True
    assert _settings(_FOREIGN_DB, _LOOPBACK_REDIS).is_remote is True
    assert _settings(_LOOPBACK_DB, _FOREIGN_REDIS).is_remote is True


def test_is_remote_false_for_unanchored_sentinel() -> None:
    """The never-dialed boot placeholder names loopback and must stay local —
    the connect guard matches it byte-for-byte and no management path may
    treat a pre-install checkout as remote."""
    assert _settings(UNANCHORED_DB_SENTINEL, _LOOPBACK_REDIS).is_remote is False


def test_is_remote_false_for_hostless_socket_url() -> None:
    """A unix-socket admin URL (no host) falls back to loopback — inherently
    local, never remote."""
    assert (
        _settings(
            "postgresql://ava@/postgres?host=/tmp/ava-pg&port=5433", _LOOPBACK_REDIS
        ).is_remote
        is False
    )


def test_is_remote_true_for_hostname_foreign_host() -> None:
    """A provider hostname (Neon-style) is just as foreign as an IP literal."""
    assert (
        _settings("postgresql://ava:pw@ep-something.aws.neon.tech/ava", _FOREIGN_REDIS).is_remote
        is True
    )


# ─── TLS: URL is the switch, AVA_DB_SSLMODE is the fallback ──────────────────


def test_sslmode_none_when_config_empty_and_url_silent() -> None:
    assert sslmode_for_url("postgresql://ava:pw@10.9.8.7:5432/ava", "") is None


def test_sslmode_injected_when_config_set_and_url_silent() -> None:
    assert sslmode_for_url("postgresql://ava:pw@10.9.8.7:5432/ava", "require") == "require"


def test_sslmode_url_wins_over_config() -> None:
    """A URL that names sslmode is the switch: injecting a kwarg would override
    the URL's conninfo param in psycopg, so the helper must stay silent even
    when config is set — a stricter URL mode can never be downgraded."""
    url = "postgresql://ava:pw@10.9.8.7:5432/ava?sslmode=verify-full"
    assert sslmode_for_url(url, "require") is None


def test_sslmode_blank_config_treated_as_empty() -> None:
    assert sslmode_for_url("postgresql://ava:pw@10.9.8.7:5432/ava", "   ") is None


# ─── pool sizing: config defaults, explicit caller sizes win ─────────────────


def test_pool_size_defaults_from_config() -> None:
    assert resolved_pool_size(None, None, 1, 2) == (1, 2)


def test_pool_size_config_tuning() -> None:
    """A SaaS connection budget tunes the default pool without code changes."""
    assert resolved_pool_size(None, None, 1, 4) == (1, 4)


def test_pool_size_explicit_wins() -> None:
    assert resolved_pool_size(2, 3, 1, 8) == (2, 3)
    assert resolved_pool_size(2, None, 1, 8) == (2, 8)


# ─── P1 (QA #867): the local owner password must never clobber a provider URL ─


def test_foreign_url_password_survives_a_nonempty_cluster_secret() -> None:
    """On a secret-bearing cluster (this deployment's own posture), the settings
    validator `_apply_data_plane_passwords` used to replace EVERY db_url
    password with the local owner password / cluster secret — which destroys a
    remote/SaaS provider's credential at every settings load, so the remote
    probe always failed auth. A foreign-host URL is authoritative: its
    password must pass through untouched."""
    s = DataPlaneSettings(
        AVA_DB_URL=_FOREIGN_DB,
        AVA_REDIS_URL=_FOREIGN_REDIS,
        AVA_CLUSTER_SECRET=_LOCAL_SECRET,
    )
    # Only the IPv4-literal hostaddr pin may be appended; the provider's
    # credential must survive byte-for-byte.
    assert s.db_url == _FOREIGN_DB + "?hostaddr=10.9.8.7", (
        "the provider credential must survive settings load"
    )


def test_loopback_url_password_still_self_heals_from_secret() -> None:
    """The local self-heal is untouched: a loopback URL on a secret cluster
    keeps re-deriving its password from the owner password / secret (the
    historical behavior)."""
    s = DataPlaneSettings(
        AVA_DB_URL=_LOOPBACK_DB,
        AVA_REDIS_URL=_LOOPBACK_REDIS,
        AVA_CLUSTER_SECRET=_LOCAL_SECRET,
    )
    assert s.db_url == (
        "postgresql://ava:" + _LOCAL_SECRET + "@127.0.0.1:5432/ava?hostaddr=127.0.0.1"
    )
