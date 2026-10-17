from base import cluster


def test_redis_admin_url_uses_admin_password_and_own_port():
    assert (
        cluster.redis_admin_url("redis://meiva_main:sek@127.0.0.1:6380/0", "redis-admin")
        == "redis://default:redis-admin@127.0.0.1:6380"
    )


def test_redis_admin_url_reads_the_configured_port():
    assert (
        cluster.redis_admin_url("redis://meiva_dev:s2@127.0.0.1:18012/0", "redis-admin-2")
        == "redis://default:redis-admin-2@127.0.0.1:18012"
    )
