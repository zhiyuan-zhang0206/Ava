"""Tests for scripts/host_ops/oob_triage.py (task #3608).

The pure layer is exercised directly: classification over both payload shapes
(the `ava status --json` hold and the raw pause-owner journal), the
phase-to-command table, the desensitization whitelist, and the degraded
reading when both sources fail. No subprocess and no network run here.

The payload fixtures mirror the pair observed on 2026-09-17 (the S3 blackout
and its post-recovery ledger); values are copied from that real dump.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import cast

import pytest

from scripts.host_ops import oob_triage

# The real post-recovery pair (2026-09-17): the status verb's compact JSON and
# the journal it reads. The paused variants below flip only the state word --
# the state the same journal carried during the blackout.
_STATUS_SAMPLE: dict[str, object] = {
    "acquired_at": "2026-09-17T00:42:14.184784+00:00",
    "driver": {
        "leader": {"argv": "zsh -c echo S3-STOP2-ISSUED", "pid": 1141471},
        "liveness": "dead",
        "root": {"argv": "zsh -c echo S3-STOP2-ISSUED", "pid": 1141471},
    },
    "maintenance": {
        "commands": {"6288": 210075},
        "drained": [6288],
        "failures": {},
        "parked": [],
        "phase": "stopping",
        "undelivered": {},
    },
    "operation": "local-pause:runner-a:1137224:aaa93de5-0a30-4536-bdb6-b185ff515605",
    "scope": "local unit; excludes independent OS-managed extras and remote hosts",
    "status": "resumed",
}


def _status_payload(
    *,
    status: str = "paused",
    phase: str | None = "stopping",
    liveness: str | None = "dead",
    failures: dict[str, str] | None = None,
    operation: object = "local-pause:runner-a:1137224:aaa93de5-0a30-4536-bdb6-b185ff515605",
    acquired_at: object = "2026-09-17T00:42:14.184784+00:00",
) -> dict[str, object]:
    payload = copy.deepcopy(_STATUS_SAMPLE)
    payload["status"] = status
    payload["operation"] = operation
    payload["acquired_at"] = acquired_at
    payload["driver"] = (
        None if liveness is None else {"liveness": liveness, "root": None, "leader": None}
    )
    if phase is None:
        payload["maintenance"] = None
    else:
        maintenance = cast("dict[str, object]", copy.deepcopy(_STATUS_SAMPLE["maintenance"]))
        maintenance["phase"] = phase
        maintenance["failures"] = {} if failures is None else failures
        payload["maintenance"] = maintenance
    return payload


def _journal_payload(
    *,
    state: str = "paused",
    phase: str | None = "stopping",
    failures: dict[str, str] | None = None,
) -> dict[str, object]:
    maintenance: dict[str, object] | None = (
        None
        if phase is None
        else {
            "phase": phase,
            "failures": {} if failures is None else failures,
            "commands": {},
            "drained": [],
            "parked": [],
            "undelivered": {},
        }
    )
    return {
        "acquired_at": "2026-09-17T00:42:14.184784+00:00",
        "driver": {"root": {"argv": "zsh -c echo S3-STOP2-ISSUED"}, "leader": None},
        "holder": "local-pause:runner-a:1137224:aaa93de5-0a30-4536-bdb6-b185ff515605",
        "maintenance": maintenance,
        "state": state,
    }


def test_orphaned_stop_class_hold_renders_the_start_command() -> None:
    result = oob_triage.classify_status(_status_payload(), host="runner-a")
    assert result.classification == "orphaned"
    assert result.hold == "present"
    assert result.source == "status"
    assert result.line == "hold=present phase=stopping driver=dead state=paused"
    assert result.command == "ssh runner-a 'cd ~/.ava/source && .venv/bin/ava start'"


def test_orphaned_pre_stop_hold_renders_the_start_command() -> None:
    result = oob_triage.classify_status(_status_payload(phase="draining"), host="runner-a")
    assert result.classification == "orphaned"
    assert result.command == "ssh runner-a 'cd ~/.ava/source && .venv/bin/ava start'"


def test_live_shepherd_reads_owned_not_orphaned() -> None:
    result = oob_triage.classify_status(_status_payload(liveness="alive"), host="runner-a")
    assert result.classification == "owned"
    assert result.line == "hold=present phase=stopping driver=alive state=paused"
    # The command table is decoupled from the classification (D-addendum):
    # a readable phase renders its command regardless of the verdict.
    assert result.command is not None


def test_failed_receipts_read_stranded_not_orphaned() -> None:
    result = oob_triage.classify_status(
        _status_payload(failures={"6288": "timeout"}), host="runner-a"
    )
    assert result.classification == "stranded"
    assert result.failures == 1


def test_missing_shepherd_is_undetermined_not_orphaned() -> None:
    result = oob_triage.classify_status(_status_payload(liveness=None), host="runner-a")
    assert result.classification == "undetermined"
    assert result.line == "hold=present phase=stopping driver=missing state=paused"


def test_unreadable_shepherd_is_undetermined() -> None:
    result = oob_triage.classify_status(_status_payload(liveness="unreadable"), host="runner-a")
    assert result.classification == "undetermined"
    assert result.line == "hold=present phase=stopping driver=unreadable state=paused"


def test_resumed_ledger_is_no_hold() -> None:
    result = oob_triage.classify_status(copy.deepcopy(_STATUS_SAMPLE), host="runner-a")
    assert result.classification == "no-hold"
    assert result.hold == "absent"
    assert result.line == "hold=absent"
    assert result.command is None


def test_inactive_status_is_no_hold() -> None:
    result = oob_triage.classify_status(_status_payload(status="inactive"), host="runner-a")
    assert result.classification == "no-hold"
    assert result.line == "hold=absent"


def test_invalid_journal_is_undetermined() -> None:
    result = oob_triage.classify_status(_status_payload(status="invalid"), host="runner-a")
    assert result.hold == "invalid"
    assert result.classification == "undetermined"
    assert result.line == "hold=invalid"
    assert result.command is None


def test_journal_fallback_is_undetermined_with_identity_placeholder() -> None:
    result = oob_triage.classify_journal(_journal_payload(), host="runner-a")
    assert result.source == "journal"
    assert result.classification == "undetermined"
    assert result.line == "hold=present phase=stopping driver=? state=paused"
    assert result.command == "ssh runner-a 'cd ~/.ava/source && .venv/bin/ava start'"


def test_resumed_journal_is_no_hold() -> None:
    result = oob_triage.classify_journal(_journal_payload(state="resumed"), host="runner-a")
    assert result.classification == "no-hold"
    assert result.line == "hold=absent"


def test_both_reads_failed_degrade_to_the_undetermined_line() -> None:
    calls: list[str] = []

    def fake_read(host: str, command: str) -> dict[str, object] | None:
        calls.append(command)
        return None

    result = oob_triage.triage("runner-a", read=fake_read)
    assert calls == [oob_triage._STATUS_COMMAND, oob_triage._JOURNAL_COMMAND]
    assert result.reachable is False
    assert result.source == "none"
    assert result.classification == "undetermined"
    assert result.line == "hold=undetermined (read failed)"
    assert result.command is None


def test_status_json_hold_is_the_primary_source() -> None:
    commands: list[str] = []

    def fake_read(host: str, command: str) -> dict[str, object] | None:
        commands.append(command)
        return {"hold": _status_payload()}

    result = oob_triage.triage("runner-a", read=fake_read)
    assert commands == [oob_triage._STATUS_COMMAND]
    assert oob_triage._STATUS_COMMAND.endswith("ava status --json")
    assert result.source == "status"
    assert result.classification == "orphaned"


def test_status_surprise_falls_back_to_the_journal_read() -> None:

    def fake_read(host: str, command: str) -> dict[str, object] | None:
        if command == oob_triage._STATUS_COMMAND:
            return {"hold": {"status": "some-unknown-word"}}
        return _journal_payload()

    result = oob_triage.triage("runner-a", read=fake_read)
    assert result.source == "journal"
    assert result.classification == "undetermined"


def test_whitelist_drops_remote_free_text() -> None:
    payload = _status_payload(phase="draining", operation="evil; touch /tmp/pwned")
    payload["driver"] = {"liveness": "dead", "root": {"argv": "SECRET-ARGV"}, "leader": None}
    result = oob_triage.classify_status(payload, host="runner-a")
    assert result.operation is None
    serialized = json.dumps(oob_triage.triage_json(result))
    assert "evil" not in serialized
    assert "SECRET-ARGV" not in serialized


def test_an_unknown_phase_is_a_structural_surprise() -> None:
    with pytest.raises(ValueError, match="unknown maintenance phase"):
        oob_triage.classify_status(_status_payload(phase="starting"), host="runner-a")


def test_plain_pause_without_maintenance_payload_still_classifies() -> None:
    result = oob_triage.classify_status(_status_payload(phase=None), host="runner-a")
    assert result.classification == "orphaned"
    assert result.line == "hold=present phase=? driver=dead state=paused"
    assert result.command is None


def test_age_seconds_counts_from_acquired_at() -> None:
    now = dt.datetime(2026, 9, 17, 2, 42, 14, 184784, tzinfo=dt.UTC).timestamp()
    result = oob_triage.classify_status(_status_payload(), host="runner-a", now=now)
    assert result.age_seconds == 7200


def test_json_contract_keys() -> None:
    result = oob_triage.classify_status(_status_payload(), host="runner-a")
    payload = oob_triage.triage_json(result)
    assert set(payload) == {
        "host",
        "source",
        "reachable",
        "hold",
        "state",
        "phase",
        "driver",
        "failures",
        "operation",
        "acquired_at",
        "age_seconds",
        "classification",
        "line",
        "command",
    }
    assert json.loads(json.dumps(payload))["line"] == result.line


def test_host_pattern_accepts_aliases_and_rejects_free_text() -> None:
    assert oob_triage._HOST_PATTERN.fullmatch("runner-a")
    assert oob_triage._HOST_PATTERN.fullmatch("user@host.example")
    assert oob_triage._HOST_PATTERN.fullmatch("a" * 253)
    assert oob_triage._HOST_PATTERN.fullmatch("a" * 254) is None
    assert oob_triage._HOST_PATTERN.fullmatch("bad host") is None
    assert oob_triage._HOST_PATTERN.fullmatch("evil;rm") is None
    # A leading dash would read as an ssh option in the rendered command.
    assert oob_triage._HOST_PATTERN.fullmatch("-oProxyCommand=x") is None
    assert oob_triage._HOST_PATTERN.fullmatch("-N") is None


def test_triage_and_render_command_defend_the_host_boundary() -> None:

    def never_read(host: str, command: str) -> dict[str, object] | None:
        raise AssertionError("a read must not start for an invalid host")

    with pytest.raises(ValueError):
        oob_triage.triage("-OProxyCommand=sh", read=never_read)
    assert oob_triage.render_command("-N", "stopping") is None


class _Proc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_read_source_success_builds_the_bounded_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> _Proc:
        calls.append((argv, kwargs))
        return _Proc(0, '{"status": "paused"}')

    monkeypatch.setattr(oob_triage.subprocess, "run", fake_run)
    payload = oob_triage._read_source(
        "runner-a", oob_triage._STATUS_COMMAND, connect_timeout_s=5.0, command_timeout_s=10.0
    )
    assert payload == {"status": "paused"}
    argv, kwargs = calls[0]
    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "runner-a",
        oob_triage._STATUS_COMMAND,
    ]
    assert kwargs["timeout"] == 10.0
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_read_source_degrades_on_every_failure(monkeypatch: pytest.MonkeyPatch) -> None:

    def run_returning(returncode: int, stdout: str):
        def fake_run(argv: list[str], **kwargs: object) -> _Proc:
            return _Proc(returncode, stdout)

        return fake_run

    def run_raising(error: BaseException):
        def fake_run(argv: list[str], **kwargs: object) -> _Proc:
            raise error

        return fake_run

    failure_runs = [
        run_returning(255, ""),  # ssh / remote command failed
        run_returning(0, "not json"),  # non-JSON stdout
        run_returning(0, "[1, 2]"),  # JSON, but not an object
        run_raising(oob_triage.subprocess.TimeoutExpired("ssh", 10.0)),
        run_raising(OSError("boom")),
    ]
    for fake_run in failure_runs:
        monkeypatch.setattr(oob_triage.subprocess, "run", fake_run)
        assert (
            oob_triage._read_source(
                "runner-a",
                oob_triage._STATUS_COMMAND,
                connect_timeout_s=5.0,
                command_timeout_s=10.0,
            )
            is None
        )


@pytest.mark.parametrize(
    "value",
    [
        "bad op",
        "bad'quote",
        'bad"quote',
        "back`tick",
        "dollar$var",
        "line\nbreak",
        "-leading-dash",
        "semi;colon",
        "slash/here",
        "hash#note",
    ],
)
def test_operation_whitelist_rejects_non_pattern_characters(value: str) -> None:
    assert oob_triage._whitelisted(value, oob_triage._OPERATION_PATTERN) is None


def test_operation_whitelist_bounds_and_accepts_the_real_shape() -> None:
    real = "local-pause:runner-a:1137224:aaa93de5-0a30-4536-bdb6-b185ff515605"
    assert oob_triage._whitelisted(real, oob_triage._OPERATION_PATTERN) == real
    assert oob_triage._whitelisted("a" * 120, oob_triage._OPERATION_PATTERN) == "a" * 120
    assert oob_triage._whitelisted("a" * 121, oob_triage._OPERATION_PATTERN) is None


@pytest.mark.parametrize("value", ["x", "-2026-09-17", " 2026-09-17T00:00:00Z", "2026/09/17"])
def test_timestamp_whitelist_rejects(value: str) -> None:
    assert oob_triage._whitelisted(value, oob_triage._TIMESTAMP_PATTERN) is None


def test_timestamp_whitelist_accepts_the_real_shape() -> None:
    real = "2026-09-17T00:42:14.184784+00:00"
    assert oob_triage._whitelisted(real, oob_triage._TIMESTAMP_PATTERN) == real
