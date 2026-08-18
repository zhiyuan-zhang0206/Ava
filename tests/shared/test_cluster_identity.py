"""Path-only cluster identity primitives.

Identity IS the home path: the display label is the basename, the OS-artifact
slug is basename + path hash, and the data-plane identifiers (db / role / ACL
user) travel inside the connection URLs as data (`identity_from_url`) — nothing
derives them from a name. These replace the retired name machinery
(cluster_name / db_name / gateway_home_for / cluster_from_home).
"""

from pathlib import Path

import pytest

from shared import cluster
from shared.config import settings


def test_default_home_is_bare_ava():
    assert cluster.default_home() == Path("~/.ava").expanduser()


def test_is_default_home_expands_and_compares():
    assert cluster.is_default_home(Path("~/.ava"))
    assert not cluster.is_default_home(Path("~/.ava-foo"))


def test_home_label_is_basename():
    assert cluster.home_label(Path("/x/y/.ava-mytask")) == ".ava-mytask"
    assert cluster.home_label(Path("~/.ava")) == ".ava"


def test_home_slug_strips_leading_dots_and_hashes_path():
    slug = cluster.home_slug(Path("/x/.ava-mytask"))
    base, _, digest = slug.rpartition("-")
    assert base == "ava-mytask"
    assert len(digest) == 8
    assert all(c in "0123456789abcdef" for c in digest)


def test_home_slug_distinguishes_same_basename():
    """Two homes sharing a basename (two worktrees named alike) must get distinct
    OS-artifact slugs — the path hash is the disambiguator."""
    a = cluster.home_slug(Path("/a/.ava-task"))
    b = cluster.home_slug(Path("/b/.ava-task"))
    assert a != b


def test_home_slug_is_deterministic():
    assert cluster.home_slug(Path("/a/.ava-task")) == cluster.home_slug(Path("/a/.ava-task"))


# ── identity_from_url: db/role/ACL identifiers are DATA carried by the URLs ──────


def test_identity_from_url_reads_username():
    assert cluster.identity_from_url("postgresql://ava_main:pw@h:5433/ava_main") == "ava_main"
    assert cluster.identity_from_url("redis://ava:pw@h:6380/0") == "ava"


def test_identity_from_url_refuses_usernameless_url():
    """Identity is READ, never guessed: a URL with no username (a wiped or
    malformed .env) must fail loudly here — a silent fallback would send the
    ensure machinery off re-affirming a wrong role/ACL user."""
    with pytest.raises(ValueError, match="carries no username"):
        cluster.identity_from_url("redis://h:6379/0")
    with pytest.raises(ValueError, match="carries no username"):
        cluster.identity_from_url("redis://:pw-only@h:6379/0")
    assert cluster.DATA_PLANE_IDENTITY == "ava"


def test_db_identity_refuses_usernameless_settings_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://h:5433/ava")
    with pytest.raises(ValueError, match="carries no username"):
        cluster.db_identity()


def test_db_and_redis_identity_read_settings_urls(monkeypatch: pytest.MonkeyPatch):
    """The runtime accessors read THIS cluster's own URLs — a historical
    identifier (prod's ava_main) keeps working until an ops rename edits the
    URLs; nothing recomputes it."""
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:pw@127.0.0.1:5433/ava_main"
    )
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava_main:pw@127.0.0.1:6380/0")
    assert cluster.db_identity() == "ava_main"
    assert cluster.redis_identity() == "ava_main"


def test_session_name_has_no_cluster_segment():
    """Sessions are `ava-<service>` — the per-home session backend already scopes
    them, so no cluster token is encoded."""
    assert cluster.session_name("gateway") == "ava-gateway"
