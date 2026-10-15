from pathlib import Path

import pytest

from base import cluster


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
    a = cluster.home_slug(Path("/a/.ava-task"))
    b = cluster.home_slug(Path("/b/.ava-task"))
    assert a != b


def test_home_slug_is_deterministic():
    assert cluster.home_slug(Path("/a/.ava-task")) == cluster.home_slug(Path("/a/.ava-task"))


def test_identity_from_url_reads_username():
    assert cluster.identity_from_url("postgresql://ava_main:pw@h:5433/ava_main") == "ava_main"
    assert cluster.identity_from_url("redis://ava:pw@h:6380/0") == "ava"


def test_identity_from_url_refuses_usernameless_url():
    with pytest.raises(ValueError, match="carries no username"):
        cluster.identity_from_url("redis://h:6379/0")
    with pytest.raises(ValueError, match="carries no username"):
        cluster.identity_from_url("redis://:pw-only@h:6379/0")
    assert cluster.DATA_PLANE_IDENTITY == "ava"


def test_identity_from_url_error_redacts_password():
    with pytest.raises(ValueError, match="carries no username") as excinfo:
        cluster.identity_from_url("redis://:FICTIONAL_CREDENTIAL_DO_NOT_USE@127.0.0.1:20028/0")
    assert "FICTIONAL_CREDENTIAL_DO_NOT_USE" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_db_identity_refuses_a_url_without_database():
    with pytest.raises(ValueError, match="names no database"):
        cluster.db_identity("postgresql://ava@h:5433")


def test_db_identity_reads_the_database_not_a_delivered_login() -> None:
    assert (
        cluster.db_identity("postgresql://ava_g3_gateway:pw@127.0.0.1:6433/ava_main") == "ava_main"
    )


def test_db_and_redis_identity_read_supplied_urls():
    assert cluster.db_identity("postgresql://ava_main:pw@127.0.0.1:5433/ava_main") == "ava_main"
    assert cluster.redis_identity("redis://ava_main:pw@127.0.0.1:6380/0") == "ava_main"


def test_session_name_has_no_cluster_segment():
    assert cluster.session_name("gateway") == "ava-gateway"
