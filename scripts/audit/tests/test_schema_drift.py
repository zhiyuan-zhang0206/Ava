from __future__ import annotations

from scripts.audit.schema_drift import (
    Drift,
    classify_column,
    classify_table,
    compare,
    render,
    schema_columns,
    strip_comments,
)


def test_strip_comments_drops_line_and_block_comments() -> None:
    sql = "CREATE TABLE a ( -- trailing\n  id INT /* inline */\n);\n-- whole line\n"
    assert strip_comments(sql) == "CREATE TABLE a ( \n  id INT \n);\n\n"


def test_strip_comments_keeps_dashes_inside_a_string_literal() -> None:
    sql = "INSERT INTO t VALUES ('a -- b', 'c /* d */');"
    assert strip_comments(sql) == sql


def test_strip_comments_drops_the_rest_of_an_unterminated_block() -> None:
    assert strip_comments("SELECT 1; /* never closed") == "SELECT 1; "


def test_schema_columns_reads_a_create_table_body() -> None:
    sql = "CREATE TABLE agents (id BIGINT, name TEXT);"
    assert schema_columns(sql) == {"agents": ["id", "name"]}


def test_schema_columns_skips_table_level_constraints() -> None:
    sql = """
    CREATE TABLE pairs (
        left_id BIGINT,
        right_id BIGINT,
        UNIQUE (left_id, right_id),
        CONSTRAINT pairs_pk PRIMARY KEY (left_id, right_id),
        CHECK (left_id <> right_id)
    );
    """
    assert schema_columns(sql) == {"pairs": ["left_id", "right_id"]}


def test_schema_columns_keeps_a_column_named_key() -> None:
    sql = "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);"
    assert schema_columns(sql) == {"kv": ["key", "value"]}


def test_schema_columns_keeps_a_check_constraint_from_ending_the_body() -> None:
    sql = "CREATE TABLE t (kind TEXT CHECK (kind IN ('a', 'b')), other INT);"
    assert schema_columns(sql) == {"t": ["kind", "other"]}


def test_schema_columns_applies_alter_table_add_column() -> None:
    sql = """
    CREATE TABLE agents_meta (agent_id BIGINT);
    ALTER TABLE agents_meta ADD COLUMN creation_key TEXT;
    ALTER TABLE ONLY agents_meta ADD COLUMN IF NOT EXISTS creation_hash TEXT;
    """
    assert schema_columns(sql) == {"agents_meta": ["agent_id", "creation_key", "creation_hash"]}


def test_schema_columns_strips_quoted_identifiers() -> None:
    sql = 'CREATE TABLE "audit" ("order" INT, "select" TEXT);'
    assert schema_columns(sql) == {"audit": ["order", "select"]}


def test_compare_reports_extra_and_missing_tables() -> None:
    drift = compare({"agents": []}, {"agents": {}, "checkpoints": {}})
    assert (drift.extra_tables, drift.missing_tables) == (["checkpoints"], [])


def test_compare_reports_columns_case_insensitively() -> None:
    schema = {"Agents": ["ID", "Name"]}
    live = {"agents": {"id": "NO|", "name": "YES|", "watchers": "NO|'[]'::jsonb"}}
    drift = compare(schema, live)
    assert drift.extra_columns == [("agents", "watchers", "NO|'[]'::jsonb")]
    assert drift.missing_columns == []


def test_compare_reports_a_declared_column_absent_from_the_database() -> None:
    drift = compare({"agents": ["id", "retired"]}, {"agents": {"id": "NO|"}})
    assert (drift.extra_columns, drift.missing_columns) == ([], [("agents", "retired")])


def test_classify_table_recognises_a_partition_of_a_declared_parent() -> None:
    assert classify_table("telemetry_events_202610", {"telemetry_events"}) == (
        "design/runtime-partition"
    )


def test_classify_table_recognises_the_default_partition() -> None:
    assert classify_table("telemetry_events_default", {"telemetry_events"}) == (
        "design/runtime-partition"
    )


def test_classify_table_does_not_invent_a_class_for_an_undeclared_parent() -> None:
    assert classify_table("telemetry_events_202610", set()) == "historical"


def test_classify_table_recognises_an_undeclared_runtime_ddl_table() -> None:
    assert classify_table("checkpoint_blobs", set()) == "design/runtime-ddl"


def test_classify_table_falls_back_to_historical() -> None:
    assert classify_table("agents_meta_2026_backup", {"agents_meta"}) == "historical"


def test_classify_column_flags_a_required_column_without_a_default() -> None:
    assert classify_column("NO|") == "dangerous"


def test_classify_column_treats_a_defaulted_column_as_historical() -> None:
    assert classify_column("NO|'[]'::jsonb") == "historical"
    assert classify_column("YES|") == "historical"


def test_render_prints_clean_without_drift() -> None:
    assert render(Drift(), set()) == "# schema drift\n\nclean"


def test_render_annotates_every_finding_with_its_class() -> None:
    drift = Drift(
        extra_tables=["checkpoints"],
        extra_columns=[("agents_meta", "watchers", "NO|'[]'::jsonb")],
        missing_columns=[("agents_meta", "retired")],
    )
    assert render(drift, {"agents_meta"}).splitlines() == [
        "# schema drift",
        "",
        "extra table   checkpoints  [design/runtime-ddl]",
        "extra column  agents_meta.watchers  [historical]",
        "missing column agents_meta.retired",
    ]
