"""Cutover journals at production scale. W7 records the before image of every
identity-less row it mints (5,433 on production), a journal of several MB, and
every later reader reads it back: the closing `--check`, the W8 held start and
W11 resume gate, a W12 run. A journal over the read ceiling refuses with its
path, size and the ceiling, never with a hint to re-run."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from cli.cutover_hold import (
    ADOPTION_JOURNAL,
    CUTOVER_JOURNAL_MAX_BYTES,
    CutoverHold,
    recorded_hold,
)
from scripts import cutover_adopt_home as adopt
from scripts import cutover_db_records as records
from scripts import cutover_inventory as inventory
from tests.lifecycle.cutover.test_db_records import (  # noqa: F401 -- fixtures
    Cluster,
    _application_owner,
    _inputs,
    _patched_session,
    _run,
    cluster,
)

# Production's identity-less terminated rows at the cutover (5,433), rounded up.
PRODUCTION_ROWS = 5_500
_MIB = 1024 * 1024


def _identityless(db: psycopg.Connection, machine: str, count: int) -> None:
    """`count` rows as the retired runtime left them: terminated, NULL resources,
    a process runtime identity and its pid, the image the mint records."""
    db.execute(
        "WITH made AS (INSERT INTO agents(label_user_set) "
        "SELECT false FROM generate_series(1, %s) RETURNING id) "
        "INSERT INTO agents_meta(id, status, machine, termination_source, runtime_kind, "
        "runtime_generation, runtime_owner, pid) SELECT id, 'terminated', %s, 'user', "
        "'process', gen_random_uuid(), gen_random_uuid(), 40000 + id FROM made",
        (count, machine),
    )
    db.commit()


def test_a_production_scale_journal_reads_back_at_every_later_step(
    cluster: Cluster,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _identityless(db_conn, cluster.runner, PRODUCTION_ROWS)
    inputs = _inputs(tmp_path, cluster)
    run = _run(cluster, inputs)  # W7 --execute
    minted = sum(effect["count"] for effect in run["effects"]["identities"])
    assert (run["state"], run["results"]["identities"], minted) == (
        "done",
        ["applied"],
        PRODUCTION_ROWS,
    )
    size = (cluster.home / records.JOURNAL).stat().st_size
    assert 3 * _MIB < size < CUTOVER_JOURNAL_MAX_BYTES // 8

    capsys.readouterr()
    with _patched_session(write=False):  # W7's closing --check
        code = records.main(["--home", str(cluster.home), "--check"])
    out = capsys.readouterr()
    assert (code, out.err) == (2, "")  # 2: the fixture's fenced rows, not an error
    assert json.loads(out.out)["checks"]["D-8"]["verdict"] == "fenced"

    assert adopt._records_repair_missing(cluster.home) is None  # the W8 and W11 gate

    w12 = replace(inputs, reason="W12", pending=None, lease=None, retire_units=())
    assert _run(cluster, w12)["state"] == "done"
    journal = records.read_journal(cluster.home)
    assert journal is not None and len(journal["runs"]) == 2


def test_a_journal_over_the_read_ceiling_refuses_with_its_size(
    cluster: Cluster, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cluster.home / records.JOURNAL
    path.parent.mkdir(parents=True)
    with path.open("wb") as stream:  # sparse: a reader sees the size before any byte
        stream.truncate(CUTOVER_JOURNAL_MAX_BYTES + 1)
    stated = re.escape(
        f"{path} is {CUTOVER_JOURNAL_MAX_BYTES + 1} bytes, over the "
        f"{CUTOVER_JOURNAL_MAX_BYTES}-byte read ceiling of a cutover journal"
    )
    with pytest.raises(records.RefusedError, match=stated):
        records.read_journal(cluster.home)
    with pytest.raises(records.RefusedError, match=stated):
        adopt._records_repair_missing(cluster.home)
    capsys.readouterr()
    with _patched_session(write=False):
        assert records.main(["--home", str(cluster.home), "--check"]) == 1
    err = capsys.readouterr().err
    assert re.search(stated, err) and "refused, nothing changed by this run" in err
    assert "re-run to continue" not in err


def test_the_adoption_journal_readers_share_the_ceiling(tmp_path: Path) -> None:
    """The runtime's hold check and the cutover scripts read the adoption journal
    alike: one past the former 1 MiB default reads in both."""
    home = tmp_path.resolve()
    hold = {"holder": "cutover:c1", "acquired_at": "2026-09-29T01:00:00+00:00"}
    effects = [
        {"op": "archive", "src": f"{index:04d}" + "x" * 1024, "area": "home-files"}
        for index in range(1024)
    ]
    journal = {
        "version": 1,
        "home": str(home),
        "cutover_id": "c1",
        "created_at": hold["acquired_at"],
        "inputs": {},
        "hold": hold,
        "steps": {"files": {"state": "done", "effects": effects}},
    }
    path = home / ADOPTION_JOURNAL
    path.parent.mkdir()
    path.write_text(json.dumps(journal))
    assert path.stat().st_size > _MIB
    assert inventory.read_journal(home) == journal
    at = datetime.fromisoformat(hold["acquired_at"])
    assert recorded_hold(home) == CutoverHold(hold["holder"], at)
