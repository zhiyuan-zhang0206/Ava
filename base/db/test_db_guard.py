import os
from urllib.parse import urlparse

_MAIN_DB_NAME = "ava_main"

_FRESH_BIRTH_DB_NAME = "ava"

_TEST_DB_NAME_PREFIXES = ("ava_citest", "ava_test")
_SENTINEL_DB_NAMES = frozenset({"unprovisioned", "run-ava-start-first"})

_TEST_MARKER_ENV = "AVA_TEST_DB"


def assert_test_db_url(db_url: str | None, *, context: str) -> None:
    if not db_url:
        raise RuntimeError(
            f"{context}: AVA_DB_URL is not set — refusing to run a test process "
            "that could resolve a production database at write time"
        )
    parsed = urlparse(db_url)
    dbname = (parsed.path or "").lstrip("/")
    host = parsed.hostname or ""
    explicit = os.environ.get(_TEST_MARKER_ENV) == "1"

    if dbname == _MAIN_DB_NAME or _MAIN_DB_NAME in host:
        raise RuntimeError(
            f"{context}: refuses to run against the production database "
            f"{dbname!r}@{host}. A test process must never write the main "
            "cluster; run with a throwaway test database (AVA_TEST_DB is "
            "deliberately NOT honored for the main cluster)."
        )
    if dbname in _SENTINEL_DB_NAMES or dbname.startswith(_TEST_DB_NAME_PREFIXES):
        return
    if dbname == _FRESH_BIRTH_DB_NAME and not explicit:
        raise RuntimeError(
            f"{context}: database {dbname!r}@{host} is the fresh-birth cluster "
            f"name, not a throwaway test database. Set {_TEST_MARKER_ENV}=1 to "
            "declare this process a test process targeting that cluster."
        )
    if not explicit:
        raise RuntimeError(
            f"{context}: refuses to run against non-test database {dbname!r} "
            f"@{host}. Point the test bootstrap at a throwaway test database "
            f"(ava_citest / ava_test*), or set {_TEST_MARKER_ENV}=1 to declare "
            "this process a test process targeting that database."
        )
