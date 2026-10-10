"""Reading `wal-verify integrity timeline --json`: only the status counts, never the exit code.

`fixtures/wal-verify-warning.json` is real v3.0.9 output taken while two segments were
still uploading: overall WARNING with exit code 0. The FAILURE cases edit its statuses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from services.backup.walg import verify
from services.backup.walg.runner import WalgCommandError
from services.backup.walg.tests.support import Sandbox, fixture_text, make_sandbox
from services.backup.walg.verify import VerifyOutputError, parse_verdict

ADMIN_URL = "postgresql://tester@/postgres?host=/sockets/ava-pg-home&port=5433"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _report(*, integrity: str, timeline: str = "OK") -> str:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = integrity
    report["timeline"]["status"] = timeline
    return json.dumps(report)


def test_segments_still_uploading_are_a_warning_not_a_failure() -> None:
    verdict = parse_verdict(fixture_text("wal-verify-warning.json"))

    assert (verdict.integrity, verdict.timeline) == ("WARNING", "OK")
    assert verdict.failed is False


def test_ok_is_not_a_failure() -> None:
    assert parse_verdict(_report(integrity="OK")).failed is False


@pytest.mark.parametrize(
    ("integrity", "timeline"), [("FAILURE", "OK"), ("OK", "FAILURE"), ("WARNING", "FAILURE")]
)
def test_a_failure_in_either_check_fails_the_chain(integrity: str, timeline: str) -> None:
    assert parse_verdict(_report(integrity=integrity, timeline=timeline)).failed is True


def test_an_exit_code_of_zero_with_a_failure_status_is_a_failed_chain(sandbox: Sandbox) -> None:
    """The measured behavior: WAL-G v3.0.9 exits 0 while reporting a gap."""
    sandbox.put("wal-verify.json", _report(integrity="FAILURE"))
    sandbox.put("wal-verify.rc", "0")

    verdict = verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)

    assert verdict.failed is True
    assert sandbox.calls() == ["wal-verify integrity timeline --json"]


def test_a_non_zero_exit_is_an_error_even_with_an_ok_report(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _report(integrity="OK"))
    sandbox.put("wal-verify.rc", "1")

    with pytest.raises(WalgCommandError):
        verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)


@pytest.mark.parametrize("status", ["PASSED", "ok", "", None])
def test_an_unknown_status_is_an_error_never_a_pass(status: str | None) -> None:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = status

    with pytest.raises(VerifyOutputError):
        parse_verdict(json.dumps(report))


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"integrity": {"status": "OK"}}', "[]"])
def test_output_without_both_checks_is_an_error(text: str) -> None:
    with pytest.raises(VerifyOutputError):
        parse_verdict(text)


def test_wal_verify_is_handed_the_owner_only_socket(sandbox: Sandbox) -> None:
    """It asks Postgres for the current segment: without PGHOST/PGPORT WAL-G dials libpq's
    default socket, which is not where this home's Postgres listens."""
    sandbox.put("wal-verify.json", _report(integrity="OK"))

    verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)

    assert sandbox.verify_env_log() == ["PGHOST=/sockets/ava-pg-home PGPORT=5433 PGUSER=tester"]
