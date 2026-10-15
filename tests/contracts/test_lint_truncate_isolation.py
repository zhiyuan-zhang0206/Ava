from __future__ import annotations

import ast
import re
from pathlib import Path

import psycopg

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISIONING = _REPO_ROOT / "tests" / "fixtures" / "provisioning.py"


def _truncate_list_from_provisioning() -> set[str]:
    tree = ast.parse(_PROVISIONING.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PER_TEST_TRUNCATE_TABLES" for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), (
                "provisioning constant must be a tuple literal"
            )
            values: set[str] = set()
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    values.add(elt.value)
            return values
    raise AssertionError("_PER_TEST_TRUNCATE_TABLES not found in tests/fixtures/provisioning.py")


_MIGRATIONS_DIR = _REPO_ROOT / "migrations"
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)")

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(text: str) -> str:
    return _SQL_BLOCK_COMMENT_RE.sub("", _SQL_LINE_COMMENT_RE.sub("", text))


_DROP_TABLE_RE = re.compile(r"DROP TABLE (?:IF EXISTS )?(?:\w+\.)?(\w+)")


def _schema_tables() -> set[str]:
    tables: set[str] = set()
    schema = _strip_sql_comments((_REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    tables.update(_CREATE_TABLE_RE.findall(schema))
    tables.update({"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"})
    for mig in sorted(_MIGRATIONS_DIR.rglob("*.sql")):
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
    closure = set(seed)
    changed = True
    while changed:
        changed = False
        for child, parent in edges:
            if parent in closure and child not in closure:
                closure.add(child)
                changed = True
    return closure


_EXEMPT: dict[str, str] = {
    "schema_migrations": "migration bookkeeping — never test data",
    "checkpoint_migrations": "LangGraph migration bookkeeping — never test data",
    "agent_metric_collection": "migration-seeded collection cutover singleton — "
    "read-only after installation, never test data",
    "cluster_defaults": "cluster singleton defaults — infra, not test data",
    "deployment_state": "R1 singleton (id=1, CHECK) — its live consumer is the "
    "code-version gate (base/db/code_version_gate.py); the row is seeded by the "
    "migration and truncating it would delete the row mid-session. Tests that write "
    "it restore it",
}


def test_truncate_list_covers_every_public_table(db_url: str) -> None:
    truncate = _truncate_list_from_provisioning()
    tables = _schema_tables()
    with psycopg.connect(db_url, autocommit=True) as conn:
        edges = _fk_edges(conn)

    covered = _cascade_closure(truncate, edges)
    uncovered = sorted(tables - covered - set(_EXEMPT))
    assert not uncovered, (
        "per-test data tables not covered by the TRUNCATE list (or FK cascade "
        f"from it, or the exemption list): {uncovered} — add them to "
        "tests/fixtures/provisioning.py _PER_TEST_TRUNCATE_TABLES or justify the exemption"
    )


def test_exemption_list_has_no_stale_entries(db_url: str) -> None:
    truncate = _truncate_list_from_provisioning()
    tables = _schema_tables()
    with psycopg.connect(db_url, autocommit=True) as conn:
        edges = _fk_edges(conn)

    covered = _cascade_closure(truncate, edges)
    stale = [t for t in _EXEMPT if t not in tables or t in covered]
    assert not stale, f"stale exemption entries (removed or now covered): {stale}"


def test_strip_sql_comments_neutralizes_comment_ddl_citations() -> None:
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
