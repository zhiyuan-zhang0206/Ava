"""`scripts/lint_termination_source.py` — the terminated-write stamping invariant.

A NULL `termination_source` is permanently unresurrectable (the
CrashResurrectController claim filters on it), so a write site that forgets to
stamp strands its agent's queued work with no error and no log. These cases pin the
two forms a real write site takes — the SQL literal and the bind parameter — plus
the reads and non-terminated writes that must NOT be flagged.
"""

from __future__ import annotations

import importlib

_lint = importlib.import_module("scripts.lint_termination_source")

_SOURCES = frozenset({"user", "exit", "reaper", "launch-confirm", "integrity"})


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(src, _SOURCES)


def test_literal_terminated_without_source_is_flagged():
    src = (
        "cur.execute(\n"
        "    \"UPDATE agents_meta SET status = 'terminated' WHERE id = %s\",\n"
        "    (agent_id,),\n"
        ")\n"
    )
    assert len(_violations(src)) == 1


def test_literal_terminated_with_source_is_clean():
    src = (
        "cur.execute(\n"
        "    \"UPDATE agents_meta SET status = 'terminated', \"\n"
        "    \"termination_source = 'launch-confirm' WHERE id = %s AND status = 'allocated'\",\n"
        "    (agent_id,),\n"
        ")\n"
    )
    assert _violations(src) == []


def test_parameterized_terminated_without_source_is_flagged():
    # The form the real defect took: status passed as a bind parameter, so a grep
    # for `status = 'terminated'` misses it entirely. The placeholder is mapped to
    # the params tuple by position.
    src = (
        "cur.execute(\n"
        '    "UPDATE agents_meta SET status = %s WHERE id = %s AND status = %s",\n'
        "    (AgentStatus.TERMINATED, agent_id, AgentStatus.ALLOCATED),\n"
        ")\n"
    )
    assert len(_violations(src)) == 1


def test_parameterized_non_terminated_status_write_is_clean():
    # enter_starting_state's own allocated -> starting flip: same SQL shape, but the
    # bound value is not TERMINATED, so it must not be dragged in.
    src = (
        "cur.execute(\n"
        '    "UPDATE agents_meta SET status = %s, pid = %s WHERE id = %s AND status = %s",\n'
        "    (AgentStatus.STARTING, os.getpid(), agent_id, AgentStatus.ALLOCATED),\n"
        ")\n"
    )
    assert _violations(src) == []


def test_terminated_only_in_where_clause_is_clean():
    # resurrect_agent's terminated -> allocated transition: TERMINATED appears as a
    # WHERE filter (a read), and the SET clause legitimately clears the source.
    src = (
        "cur.execute(\n"
        '    "UPDATE agents_meta SET status = %s, termination_source = NULL "\n'
        '    "WHERE id = %s AND status = %s",\n'
        "    (AgentStatus.ALLOCATED, agent_id, AgentStatus.TERMINATED),\n"
        ")\n"
    )
    assert _violations(src) == []


def test_terminated_write_setting_source_null_is_flagged():
    # Explicitly writing NULL is the same hole as omitting the stamp: NULL means
    # "pre-column legacy row", not "deliberately not resurrectable".
    src = (
        "cur.execute(\n"
        "    \"UPDATE agents_meta SET status = 'terminated', termination_source = NULL \"\n"
        '    "WHERE id = %s",\n'
        "    (agent_id,),\n"
        ")\n"
    )
    assert len(_violations(src)) == 1


def test_unknown_source_value_is_flagged():
    # A value outside TerminationSource would fail the column's CHECK at runtime,
    # against a real database only.
    src = (
        "cur.execute(\n"
        "    \"UPDATE agents_meta SET status = 'terminated', \"\n"
        "    \"termination_source = 'crashed' WHERE id = %s\",\n"
        "    (agent_id,),\n"
        ")\n"
    )
    assert len(_violations(src)) == 1


def test_multirow_reaper_claim_with_source_is_clean():
    # The reapers' set-based claim: a subquery + RETURNING, still one statement that
    # stamps status and source together.
    src = (
        "cur.execute(\n"
        "    \"UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' \"\n"
        "    \"WHERE machine = %s AND status = 'starting' RETURNING id\",\n"
        "    (local_machine,),\n"
        ")\n"
    )
    assert _violations(src) == []


def test_other_table_is_ignored():
    src = "cur.execute(\"UPDATE inbound_messages SET status = 'terminated' WHERE id = %s\", (i,))\n"
    assert _violations(src) == []


def test_dynamic_sql_is_skipped():
    # A query built at runtime is unknowable statically; skipping is the honest
    # answer (and no current site does this).
    src = 'cur.execute(build_query("agents_meta"), (agent_id,))\n'
    assert _violations(src) == []


def test_real_tree_has_zero_violations():
    # Every terminated-write site in the live framework tree stamps a source.
    assert _lint.main() == 0
