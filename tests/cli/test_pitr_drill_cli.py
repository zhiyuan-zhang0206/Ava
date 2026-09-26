"""`ava pitr drill` parse-layer gates: chain/candidate XOR + target-wall format (task #4092 B4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.main import _build_parser

_TARGET = ["--target-lsn", "26/A03520B0", "--target-wall", "2026-09-13 13:13:03+08"]


def test_requires_exactly_one_candidate_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(["pitr", "drill", *_TARGET, "--scratch", "./s"])
    assert raised.value.code == 2
    assert "one of the arguments --chain --candidate is required" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--candidate",
                "./m.json",
                *_TARGET,
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_target_wall_is_validated_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--target-lsn",
                "26/A",
                "--target-wall",
                "soon",
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "argument --target-wall:" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--target-lsn",
                "26/A",
                "--target-wall",
                "2026-09-13 13:13:03",
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "UTC offset" in capsys.readouterr().err


def test_valid_invocation_parses() -> None:
    ns = _build_parser().parse_args(["pitr", "drill", "--chain", "c", *_TARGET, "--scratch", "./s"])
    assert ns.chain == "c" and ns.candidate is None
    assert ns.target_wall == "2026-09-13 13:13:03+08"


def test_operations_verbs_parse() -> None:
    assert _build_parser().parse_args(["pitr", "operations", "retire", "--confirm"]).confirm
    assert _build_parser().parse_args(["pitr", "operations", "status"]).operations_cmd == "status"


def _drill_cli(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    from cli.commands import pitr as commands
    from services.pitr import base_operation_runtime as runtime
    from tests.services.test_pitr_base_scheduler import _candidate

    def resolve(_chain: object, _candidate_path: object) -> object:
        return _candidate("c")

    def key(_config: object) -> Path:
        return Path("/key")

    def store_args(_config: object) -> tuple[tuple[str, str], ...]:
        return ()

    monkeypatch.setattr(commands, "_resolve_drill_candidate", resolve)
    monkeypatch.setattr(commands, "direct_db_url", lambda: "postgresql://live")
    monkeypatch.setattr(commands, "pg_tool", Path)
    monkeypatch.setattr(runtime, "live_data_directory", lambda: "/live/data")
    monkeypatch.setattr(runtime, "restore_key_path", key)
    monkeypatch.setattr(runtime, "restore_store_args", store_args)

    async def drill(_inputs: object, **kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        kwargs["progress"]("base extracted")
        return {
            "outcome": "pass",
            "chain_id": "c",
            "target_lsn": "0/180",
            "timings": {"base_download_seconds": 1.0},
            "criteria": {"counts_restored": {"agents": 2}, "business_rows": [["1", "secret"]]},
        }

    monkeypatch.setattr(runtime, "run_drill_input", drill)


def test_relative_scratch_means_the_operator_cwd_and_output_omits_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.pitr import cmd_pitr_drill

    captured: dict[str, Any] = {}
    _drill_cli(monkeypatch, captured)
    monkeypatch.chdir(tmp_path)
    code = cmd_pitr_drill(
        chain="c",
        candidate=None,
        target_lsn="0/180",
        target_wall="2026-09-26 00:00:00+00",
        scratch="rel-scratch",
        timeout_seconds=5,
    )
    out, err = capsys.readouterr()
    assert code == 0
    assert captured["scratch"] == tmp_path.absolute() / "rel-scratch"
    assert "base extracted" in err and str(tmp_path / "rel-scratch") in err
    summary = json.loads(out)
    assert summary["evidence"] == str(tmp_path.absolute() / "rel-scratch" / "drill-evidence.json")
    assert summary["counts_restored"] == {"agents": 2} and "secret" not in out


def test_operations_retire_previews_then_releases_a_proven_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess
    import sys

    import psutil

    from cli.commands import pitr as commands
    from services.pitr import operation_custody as custody
    from shared.native_process import native_boot_id

    kind = custody.OperationKind("test", tmp_path / "controls", tmp_path / "quarantine")
    work = kind.control_root / ".operation-dead"
    work.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    native = custody.NativeProcess.capture(psutil.Process(process.pid))
    process.wait(timeout=10)
    (work / "operation.json").write_text(json.dumps({"boot_id": native_boot_id()}))
    (work / "worker.json").write_text(json.dumps({"pid": process.pid, "native": native.value()}))
    monkeypatch.setattr(commands, "_operation_kinds", lambda: [kind])
    assert commands.cmd_pitr_operations_status() == 1
    assert commands.cmd_pitr_operations_retire(confirm=False) == 0
    assert "closure proven" in capsys.readouterr().out and work.is_dir()
    assert commands.cmd_pitr_operations_retire(confirm=True) == 0
    assert "retired into" in capsys.readouterr().out and not work.exists()
    assert commands.cmd_pitr_operations_status() == 0


def test_operations_discard_candidate_clears_a_stale_weekly_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leftover weekly `.ready` blocks activation's forced candidate; the
    operator clears it explicitly, never while an operation still owns it."""
    from cli.commands import pitr as commands

    monkeypatch.setattr(commands, "ava_home", lambda: tmp_path)
    root, chain = tmp_path / "physical-backup", "20260920T030000Z"
    ready = root / "base-candidates" / f"{chain}.ready"
    (ready / "base").mkdir(parents=True)
    facts = root / "base-facts" / f"{chain}.json"
    for staged in (facts, root / "base-plans" / f"{chain}.plan.json"):
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("{}")
    assert commands.cmd_pitr_operations_discard_candidate(chain=chain, confirm=False) == 0
    assert ready.is_dir() and "would discard" in capsys.readouterr().out
    owner = root / "base-facts" / f"{chain}.owner.json"
    owner.write_text("{}")
    assert commands.cmd_pitr_operations_discard_candidate(chain=chain, confirm=True) == 1
    assert ready.is_dir() and "still owns" in capsys.readouterr().err
    owner.unlink()
    assert commands.cmd_pitr_operations_discard_candidate(chain=chain, confirm=True) == 0
    assert not ready.exists() and not facts.exists()
    args = _build_parser().parse_args(
        ["pitr", "operations", "discard-candidate", chain, "--confirm"]
    )
    assert (args.chain, args.confirm) == (chain, True)
