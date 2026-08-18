"""Fail-fast guard: no test process may resolve a production DB URL.

Why this exists
---------------
2026-08-12 17:57 incident: a pytest run of the (then in-repo) benchmark stats
tests wrote 8 synthetic agents (ids 900002-900010, labels ``test-agent-<id>``)
into the production ``agents`` / ``agents_meta`` tables. The run's rootdir was
outside this repo, so Ava's ``tests/conftest.py`` — whose import-time env
block redirects AVA_HOME to a tmp home and pins ``AVA_DB_URL`` to an
unreachable sentinel — never loaded.
``shared.config.settings`` then resolved the operator's real ``~/.ava/.env``,
and the test helper that seeds agent rows wrote straight into the main
cluster's database. The per-helper guard that existed
(``tests/ava/conftest.py::_ensure_agents_meta_row``) was on a different seed
path in a different conftest tree and never ran.

The rule below is the single source of truth for "is this database one a test
process may write". Every pytest bootstrap that embeds this codebase calls it at
session start — ``tests/conftest.py`` (pytest_sessionstart, before any
fixture) — and the DB-seeding test helpers call it
before touching a connection. Fail-closed: a URL that is neither a known
throwaway test database nor explicitly marked resolves to a refusal, so a
bootstrap that stops loading a conftest (or a new harness that never had one)
fails loudly instead of polluting production.
"""

import os
from urllib.parse import urlparse

# The main cluster's database — the historical name carried by ~/.ava/.env
# (AGENTS.md: "prod stays on its historical ava_main"). No test process ever
# legitimately targets it, so the refusal is unconditional.
_MAIN_DB_NAME = "ava_main"

# A fresh cluster birth writes the fixed name "ava" (AGENTS.md). That includes
# worktree dev clusters, which a test bootstrap MAY target deliberately — but
# never implicitly, so it needs the explicit AVA_TEST_DB=1 marker.
_FRESH_BIRTH_DB_NAME = "ava"

# The suite's throwaway Postgres (tests/_containers.py ->
# shared/pg_tools.throwaway_postgres) provisions databases named ava_citest;
# ava_test* is the namespace for any per-session test database. The sentinel
# URLs have no server behind them (port 1 on loopback) — every write fails
# loudly, so they are safe to allow: "unprovisioned" is what the Ava suite pins
# at import, "run-ava-start-first" is dotenv_boot's unanchored-dev-checkout
# sentinel (shared/dotenv_boot.UNANCHORED_DB_SENTINEL), which a harness whose
# bootstrap skips the env block lands on instead of the prod URL.
_TEST_DB_NAME_PREFIXES = ("ava_citest", "ava_test")
_SENTINEL_DB_NAMES = frozenset({"unprovisioned", "run-ava-start-first"})

# Env marker a test bootstrap sets to declare "this process is a test process
# and the resolved DB is my deliberate test target".
_TEST_MARKER_ENV = "AVA_TEST_DB"


def assert_test_db_url(db_url: str | None, *, context: str) -> None:
    """Raise RuntimeError unless ``db_url`` is a database a test may write.

    Rules (fail-closed):

    - Database name ``ava_main`` (the main cluster) — or a hostname carrying
      it — is refused unconditionally, even with ``AVA_TEST_DB=1``.
    - The fresh-birth name ``ava`` is refused unless ``AVA_TEST_DB=1`` is set
      (a worktree dev cluster is a legitimate deliberate test target).
    - Throwaway test names (``ava_citest`` / ``ava_test*``) and the suite's
      unreachable sentinel (``unprovisioned``) are allowed.
    - Any other database name is refused unless ``AVA_TEST_DB=1`` is set.

    ``context`` names the caller in the error message (e.g. "pytest session",
    the seeding helper) so a refusal points at the bootstrap that leaked.
    """
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
