"""`--execute` enforces the check's verdicts: a check reading `attention`
refuses the run, and the rows it leaves fenced are stated before the first
write, recorded in the journal and keep `--check` from reading clean."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
import json
from pathlib import Path

import psycopg
import pytest

from scripts import cutover_db_records as records
from scripts import cutover_db_survey as survey_module
from tests.lifecycle.cutover.test_db_records import (  # noqa: F401 -- fixtures
    _BOOTSTRAP_OWNED,
    Cluster,
    _application_owner,
    _inputs,
    _patched_session,
    _refusals,
    _run,
    _state,
    _survey,
    cluster,
)


def test_rows_left_fenced_keep_d8_and_the_check_from_reading_clean(
    cluster: Cluster,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(cluster, _inputs(tmp_path, cluster))
    capsys.readouterr()
    with _patched_session(write=False):
        code = records.main(["--home", str(cluster.home), "--check"])
    checks = json.loads(capsys.readouterr().out)["checks"]
    unclean = {
        name: c["verdict"] for name, c in checks.items() if c["verdict"] not in ("ok", "info")
    }
    assert (code, unclean) == (2, {"D-8": "fenced"})
    agents = cluster.agents
    assert checks["D-8"]["fenced"] == [
        {
            "verdict": "inadmissible",
            "reason": "no settled lifecycle receipt for the recorded incarnation",
            "count": 1,
            "agents": [agents["unreceipted"]],
        },
        {
            "verdict": "inadmissible",
            "reason": "the machine is paused; no closure evidence exists for it",
            "count": 1,
            "agents": [agents["paused"]],
        },
        {
            "verdict": "inadmissible",
            "reason": "the row still names a live or different incarnation",
            "count": 1,
            "agents": [agents["live"]],
        },
    ]


def test_execute_refuses_while_any_check_reads_attention(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attention verdict is a premise to resolve first, never a note."""
    monkeypatch.setattr(survey_module, "_OWNER", _BOOTSTRAP_OWNED)
    unchanged = _state(db_conn)
    head = records.resolve_commit("HEAD")
    inputs = _inputs(tmp_path, cluster, legacy_commit=head)
    blocked = [
        "D-3 reads attention: the cluster pin names no commit, not --legacy-commit " + head,
        "D-10 reads attention: the database is owned by the bootstrap superuser "
        + _survey(inputs).checks["D-10"]["database_owner"],
    ]
    assert _refusals(cluster, inputs) == blocked
    with pytest.raises(records.RefusedError) as refused:
        _run(cluster, inputs)
    assert str(refused.value) == "; ".join(blocked)
    assert _state(db_conn) == unchanged
    assert not (cluster.home / records.JOURNAL).exists()


def test_execute_prints_and_records_what_stays_fenced(
    cluster: Cluster, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before its first write the run states which agents stay fenced; the
    journal keeps that statement beside the inputs."""
    inputs = _inputs(tmp_path, cluster)
    expected = survey_module.fenced_summary(_survey(inputs).legacy)
    assert [group["count"] for group in expected] == [1, 1, 1]
    capsys.readouterr()
    run = _run(cluster, inputs)
    assert run is not None and run["fenced"] == expected
    printed = capsys.readouterr().out
    agents = cluster.agents
    assert "3 agent(s) stay fenced" in printed
    for name, reason in (
        ("unreceipted", "no settled lifecycle receipt for the recorded incarnation"),
        ("paused", "the machine is paused; no closure evidence exists for it"),
        ("live", "the row still names a live or different incarnation"),
    ):
        assert f"1 inadmissible: {reason} (agents {agents[name]})" in printed
    journal = json.loads((cluster.home / records.JOURNAL).read_text())
    assert journal["runs"][0]["fenced"] == expected
