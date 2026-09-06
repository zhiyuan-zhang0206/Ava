"""The per-test TRUNCATE list stays in sync with the schema.

tests/conftest.py truncates a hand-written table list before every test
(`_PER_TEST_TRUNCATE_TABLES`). A migration adding a new per-test data table
that nobody adds to that list silently shares state across tests — the R1
`deployment_state` near-miss and today's `agent_watchers` (audit round-2
cc-docs-tests P2) are the shape of that drift. This test derives the real
isolation closure from the live test DB and fails when a public table is
outside it:

    covered = TRUNCATE list ∪ FK-cascade closure ∪ explicit exemptions

Caveat: TRUNCATE ... CASCADE covers tables with a foreign key to a truncated
table (transitively) — e.g. `events_default` is a partition of `events`, and
`agent_pages`/`agent_tasks` reference `agents`. Only tables with no such
path need to be listed or exempted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import psycopg

from shared.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


def _truncate_list_from_conftest() -> set[str]:
    """AST-parse `_PER_TEST_TRUNCATE_TABLES` from the root conftest — the same
    source the SQL is built from, so the guard can never drift from it."""
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PER_TEST_TRUNCATE_TABLES" for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), "conftest constant must be a tuple literal"
            values: set[str] = set()
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    values.add(elt.value)
            return values
    raise AssertionError("_PER_TEST_TRUNCATE_TABLES not found in tests/conftest.py")


_MIGRATIONS_DIR = _REPO_ROOT / "migrations"
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)")

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(text: str) -> str:
    """Strip ``--`` line and ``/* */`` block comments from SQL text.

    Comment prose must never be mistaken for DDL: db/schema.sql cites a
    ``CREATE TABLE`` inside a comment (the agent_notices migration note), and
    the raw findall below once captured that citation as a real table.
    """
    return _SQL_BLOCK_COMMENT_RE.sub("", _SQL_LINE_COMMENT_RE.sub("", text))


_DROP_TABLE_RE = re.compile(r"DROP TABLE (?:IF EXISTS )?(?:\w+\.)?(\w+)")


def _schema_tables() -> set[str]:
    """Authoritative per-test table list, immune to parallel-test noise.

    The live test DB also contains throwaway tables other tests create on the
    fly (e.g. test_migrations' lock_t) — under xdist those surface in
    information_schema and made this guard flaky. The schema's own
    declarations (db/schema.sql + migration ups, replayed in order so tables
    later dropped by a migration are excluded) are the authoritative list of
    tables that can carry test data.
    """
    tables: set[str] = set()
    schema = _strip_sql_comments((_REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    tables.update(_CREATE_TABLE_RE.findall(schema))
    # LangGraph owns these declarations outside db/schema.sql. Three carry
    # per-test checkpoint data; checkpoint_migrations is infra bookkeeping.
    tables.update({"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"})
    for mig in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if mig.name.endswith(".down.sql"):
            continue
        text = _strip_sql_comments(mig.read_text(encoding="utf-8"))
        tables.update(_CREATE_TABLE_RE.findall(text))
        tables.difference_update(_DROP_TABLE_RE.findall(text))
    return tables


def _public_tables(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row[0] for row in cur.fetchall()}


def _fk_edges(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(child_table, parent_table) for every FK in the public schema."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_schema = tc.constraint_schema
             AND ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
            """
        )
        return [(child, parent) for child, parent in cur.fetchall()]


def _cascade_closure(seed: set[str], edges: list[tuple[str, str]]) -> set[str]:
    """TRUNCATE <seed> CASCADE truncates every table FK-referencing a truncated
    table, transitively — compute that closure."""
    closure = set(seed)
    changed = True
    while changed:
        changed = False
        for child, parent in edges:
            if parent in closure and child not in closure:
                closure.add(child)
                changed = True
    return closure


# Tables deliberately outside the per-test TRUNCATE blast radius, with the
# reason each one is safe. A table must be added to one of: the TRUNCATE
# list, an FK-cascade from it, or this list.
_EXEMPT: dict[str, str] = {
    "schema_migrations": "migration bookkeeping — never test data",
    "checkpoint_migrations": "LangGraph migration bookkeeping — never test data",
    "cluster_pin": "cluster singleton state — infra, not test data",
    "cluster_last_update": "cluster singleton outcome row — infra (mirrored into "
    "deployment_state by the R1 migration)",
    "cluster_defaults": "cluster singleton defaults — infra, not test data",
    "deployment_state": "R1 singleton (id=1, CHECK) — UPDATE-only consumers "
    "(shared/cluster_lock.py), row seeded by the migration; truncating it would "
    "delete the row mid-session. Tests self-clean via acquire/release pairs",
}


def test_truncate_list_covers_every_public_table() -> None:
    """Every public table is truncated per test (directly, via FK cascade, or
    explicitly exempted) — a new table silently added by a migration fails
    this guard instead of leaking state across tests."""
    truncate = _truncate_list_from_conftest()
    tables = _schema_tables()
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        edges = _fk_edges(conn)

    covered = _cascade_closure(truncate, edges)
    uncovered = sorted(tables - covered - set(_EXEMPT))
    assert not uncovered, (
        "per-test data tables not covered by the TRUNCATE list (or FK cascade "
        f"from it, or the exemption list): {uncovered} — add them to "
        "tests/conftest.py _PER_TEST_TRUNCATE_TABLES or justify the exemption"
    )


def test_exemption_list_has_no_stale_entries() -> None:
    """An exemption whose table no longer exists (or is now covered by the
    TRUNCATE list) must be dropped — the exemption list is a liability ledger."""
    truncate = _truncate_list_from_conftest()
    tables = _schema_tables()
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        edges = _fk_edges(conn)

    covered = _cascade_closure(truncate, edges)
    stale = [t for t in _EXEMPT if t not in tables or t in covered]
    assert not stale, f"stale exemption entries (removed or now covered): {stale}"


def test_strip_sql_comments_neutralizes_comment_ddl_citations() -> None:
    """Comment prose citing DDL must not reach the CREATE/DROP findalls."""
    text = (
        "-- inline in the agent_notices CREATE TABLE, because agent_notices is defined\n"
        "CREATE TABLE real_table (id BIGSERIAL);\n"
        "/* block comment: DROP TABLE ghost_block; */\n"
        "CREATE TABLE other_real (id BIGSERIAL);\n"
    )
    cleaned = _strip_sql_comments(text)
    assert "agent_notices CREATE TABLE" not in cleaned
    assert "ghost_block" not in cleaned
    assert _CREATE_TABLE_RE.findall(cleaned) == ["real_table", "other_real"]
