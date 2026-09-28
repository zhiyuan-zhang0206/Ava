"""`--execute` enforces the check's verdicts: a check reading `attention`
refuses the run, and the rows it leaves fenced are stated before the first
write, recorded in the journal and keep `--check` from reading clean."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from scripts import cutover_db_records as records
from scripts import cutover_db_survey as survey_module
from shared.config import settings
from tests.lifecycle.cutover.test_db_records import (  # noqa: F401 -- fixtures
    _BOOTSTRAP_OWNED,
    GONE,
    Cluster,
    _agent,
    _application_owner,
    _inputs,
    _patched_session,
    _refusals,
    _retired,
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


def test_a_row_no_attestation_can_reach_is_fenced_and_d8_reaches_fenced(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A row on a machine whose every unit the run retires can never be attested:
    the run states and records it as fenced, and afterwards D-8 reads `fenced`,
    not `repair` for good."""
    gone, _ = _agent(db_conn, GONE, resources=_retired(uuid4(), uuid4(), 4343), receipt="restart")
    db_conn.commit()
    inputs = _inputs(tmp_path, cluster)
    unattestable = {
        "verdict": "inadmissible",
        "reason": "no unit of its machine remains to attest (retired or unregistered)",
        "count": 1,
        "agents": [gone],
    }
    capsys.readouterr()
    run = _run(cluster, inputs)
    assert run is not None and unattestable in run["fenced"]
    assert "4 agent(s) stay fenced" in capsys.readouterr().out
    after = _survey(replace(inputs, retire_units=())).checks["D-8"]
    assert (after["verdict"], unattestable in after["fenced"]) == ("fenced", True)


def test_a_held_row_lock_fails_the_effect_and_the_same_inputs_continue(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover transaction (a prepared one survives restarts: D-11) holding
    a row a repair locks fails that effect within the lock ceiling instead of
    hanging the run; nothing of the effect commits and the run continues once
    the holder is gone."""
    monkeypatch.setattr(records, "LOCK_TIMEOUT", "200ms")
    inputs = _inputs(tmp_path, cluster)
    unchanged = _state(db_conn)
    holder = psycopg.connect(settings.data_plane.db_url)
    holder.execute("SELECT 1 FROM deployment_state WHERE id=1 FOR UPDATE")
    release = threading.Timer(5.0, holder.rollback)
    release.start()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=r"pending-clear: waited longer than lock_timeout"):
            _run(cluster, inputs)
        assert time.monotonic() - started < 5.0
    finally:
        release.cancel()
        holder.rollback()
        holder.close()
    assert _state(db_conn) == unchanged
    stopped = json.loads((cluster.home / records.JOURNAL).read_text())["runs"][0]
    assert (stopped["state"], stopped["results"]["pending"]) == ("started", [])
    run = _run(cluster, inputs)
    assert run is not None and run["state"] == "done"


def test_every_owner_session_carries_the_lock_and_statement_ceilings() -> None:
    for write in (False, True):
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            records.prepare_session(conn, write=write)
            ceilings = conn.execute(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            ).fetchone()
        assert ceilings == ("10s", "1min")
