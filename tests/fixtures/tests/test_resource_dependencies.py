import pytest


def test_failed_setup_and_late_effects_preserve_resource_windows(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import os
import pytest

pytest_plugins = [
    "tests.fixtures.env_bootstrap",
    "tests.fixtures.leak_guard",
    "tests.fixtures.identity_restore",
    "tests.fixtures.plugin_registrations",
    "tests.fixtures.static_environment",
    "tests.fixtures.provisioning",
    "tests.fixtures.guards",
]

@pytest.fixture
def failed_database(db_url):
    import psycopg
    with psycopg.connect(db_url) as conn:
        conn.execute("INSERT INTO user_settings (key) VALUES ('failed-setup')")
    raise RuntimeError("database-setup-failed")

@pytest.fixture
def failed_redis(redis_url):
    import redis
    with redis.Redis.from_url(redis_url) as client:
        client.set("failed-setup", "retained")
    raise RuntimeError("redis-setup-failed")

@pytest.fixture(scope="session")
def drained_tail(provisioned_db):
    yield
    import psycopg
    # Session ownership outlives the function's driver and URL windows. A real
    # producer's queued tail must have reached Postgres before that window shut.
    with psycopg.connect(provisioned_db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM telemetry_events WHERE machine = 'resource-tail'"
        ).fetchone() == (1,)

@pytest.fixture
def tail_pipeline(database, process_config):
    from pathlib import Path
    from base import telemetry
    from base.cluster.machine.boot import machine_from_boot
    machine = machine_from_boot(process_config)
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine, home=Path(os.environ["MEIVA_HOME"])
    )
    try:
        yield pipeline
    finally:
        pipeline.stop(timeout=2)
"""
    )
    pytester.makepyfile(
        """
import os
from pathlib import Path
import pytest

def test_00_late_host_boundaries():
    from base.host.env import bootstrap
    from base.clock.tests.manual_timers import ManualTimers
    from base.sessions.tests.refuse_helper import refuse_helper
    from services.desktop import permissions_helper
    with pytest.raises(AssertionError, match="would leave the test process"):
        bootstrap.dial_get("http://127.0.0.1:1/api/bootstrap")
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=refuse_helper, timers=ManualTimers())
    with pytest.raises(AssertionError, match="would replace the pytest process"):
        os.execv("/nonexistent-test-executable", [])

def test_10_failed_database(failed_database):
    raise AssertionError("setup must fail first")

def test_11_clean_database(db_url):
    import psycopg
    import redis
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM user_settings").fetchone() == (0,)
    with redis.Redis.from_url("redis://127.0.0.1:1/0") as client:
        with pytest.raises(pytest.fail.Exception, match="Unrequested Redis"):
            client.ping()

def test_12_no_data_plane(process_config):
    import threading
    assert not any(t.name.startswith("pg-stall-watchdog-") for t in threading.enumerate())
    from base.config import settings
    from psycopg import connect
    from psycopg.conninfo import conninfo_to_dict
    from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL, UNPROVISIONED_REDIS_URL
    assert os.environ["MEIVA_DB_URL"] == UNPROVISIONED_DB_URL
    assert os.environ["MEIVA_REDIS_URL"] == UNPROVISIONED_REDIS_URL
    assert conninfo_to_dict(settings.data_plane.db_url)["dbname"] == "unprovisioned"
    assert settings.data_plane.redis_url == UNPROVISIONED_REDIS_URL
    assert UNPROVISIONED_DB_URL in (Path(os.environ["MEIVA_HOME"]) / ".env").read_text()
    with pytest.raises(pytest.fail.Exception, match="Unrequested Postgres"):
        connect("postgresql://127.0.0.1:1/unused")

def test_20_failed_redis(failed_redis):
    raise AssertionError("setup must fail first")

def test_21_clean_redis(redis_url):
    import psycopg
    import redis
    with redis.Redis.from_url(redis_url) as client:
        assert client.get("failed-setup") is None
    with pytest.raises(pytest.fail.Exception, match="Unrequested Postgres"):
        psycopg.connect("postgresql://127.0.0.1:1/unused")

def test_30_queued_telemetry(db_url, drained_tail, tail_pipeline):
    from base import telemetry
    from base.telemetry import event_store
    event_store.set_enabled(enabled=True)
    telemetry.init_telemetry(pipeline=tail_pipeline, machine_reader=lambda: "resource-tail")
    telemetry.emit("log", "log", level="error", attributes={"tail": "must-drain"})
    # Do not sync here: the resource owner must drain before revoking access.

def test_31_refusal_after_telemetry(process_config):
    test_12_no_data_plane(process_config)
"""
    )
    result = pytester.runpytest_subprocess("-q", "-o", "addopts=")
    result.assert_outcomes(passed=6, errors=2)
    assert "database-setup-failed" in result.stdout.str()
    assert "redis-setup-failed" in result.stdout.str()
