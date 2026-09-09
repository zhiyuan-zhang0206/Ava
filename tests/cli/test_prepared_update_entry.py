"""Prepared CLI validates a retained request and refuses before shared-state effects."""

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cli import main
from cli.prepared_update import (
    PreparedOperatorInput,
    _image,
    prepare_operator_input,
    run_prepared_update,
)
from shared.managed_writer_observation import ExpectedUnitWriters
from shared.managed_writer_publication import PublishedUnit
from shared.runtime_publication_input import PreparationReceipt, PreparedService
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


@pytest.mark.parametrize(
    "tail",
    [
        ["--prepared", "/absent"],
        ["--local", "--prepared=/absent", "--force"],
        ["--local", "--prepared=/absent", "--restart-only"],
        ["--local", "--prepared=/absent", "--dry-run"],
        ["--local", "--prepared=/absent", "--mode", "force"],
    ],
)
def test_invalid_combination_refuses_before_plan_or_old_dispatch(
    monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    def forbidden(_path: Path) -> PreparedOperatorInput:
        raise AssertionError("invalid flags must not read a plan")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", forbidden)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", *tail]) == 2


def test_prepared_enters_handler_before_checkout_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def prepared(args: argparse.Namespace) -> int:
        seen.append(args.prepared)
        return 17

    def forbidden(_verb: str) -> None:
        raise AssertionError("wheel entry must not ask an absent checkout for its home")

    monkeypatch.setattr("cli.prepared_update.run_prepared_update", prepared)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", "--local", "--prepared", "/private/plan"]) == 17
    assert seen == ["/private/plan"]


def test_source_cannot_impersonate_retained_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli.prepared_update.WHEEL_RUNTIME", False)
    with pytest.raises(ReleaseRejectedError, match="retained POSIX"):
        prepare_operator_input(Path("/must-not-be-read"))


def test_deadline_expiring_during_image_validation_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "unit"
    root = home / "releases" / ("b" * 64)
    prefix = root / "venv"
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    unit = SimpleNamespace(machine="runner", home=str(home), artifact_digest=root.name)
    recovery_unit = SimpleNamespace(machine="runner", home=str(home), artifact_digest="c" * 64)
    local = SimpleNamespace(
        unit=unit,
        recovery=recovery_unit,
        recovery_schema_digest="e" * 64,
    )
    request = SimpleNamespace(
        units=(local,),
        valid_until=deadline,
        normal=SimpleNamespace(schema_digest="d" * 64),
    )
    candidate = VerifiedRelease(
        digest=root.name,
        manifest_digest="f" * 64,
        root=root,
        interpreter=prefix / "bin/python",
        cwd=root,
    )
    recovery = candidate.__class__(
        digest="c" * 64,
        manifest_digest="a" * 64,
        root=home / "releases" / ("c" * 64),
        interpreter=home / "releases" / ("c" * 64) / "venv/bin/python",
        cwd=home / "releases" / ("c" * 64),
    )
    moments = iter((deadline - timedelta(seconds=1), deadline + timedelta(seconds=1)))

    class AdvancingClock:
        @classmethod
        def now(cls, timezone: object) -> datetime:
            assert timezone is UTC
            return next(moments)

    def private_plan(_path: Path, _home: Path) -> bytes:
        return b"sealed"

    def loaded_plan(_raw: bytes | str) -> object:
        return request

    def machine_identity(_path: Path) -> bytes:
        return b"runner\n"

    def schema_digest(_path: Path) -> str:
        return "d" * 64

    def loaded_image(selected: object, _schema: str) -> tuple[VerifiedRelease, PreparationReceipt]:
        return (
            candidate if selected is unit else recovery,
            cast("PreparationReceipt", object()),
        )

    monkeypatch.setattr("cli.prepared_update.WHEEL_RUNTIME", True)
    monkeypatch.setattr("cli.prepared_update.sys.platform", "linux")
    monkeypatch.setattr("cli.prepared_update.runtime_venv", lambda: prefix)
    monkeypatch.setattr("cli.prepared_update.__file__", str(prefix / "cli/prepared_update.py"))
    monkeypatch.setattr("cli.prepared_update._private_plan", private_plan)
    monkeypatch.setattr("cli.prepared_update.PreparedOperatorPlan.model_validate_json", loaded_plan)
    monkeypatch.setattr("cli.prepared_update.regular_bytes", machine_identity)
    monkeypatch.setattr("cli.prepared_update.file_sha256", schema_digest)
    monkeypatch.setattr("cli.prepared_update._image", loaded_image)
    monkeypatch.setattr("cli.prepared_update.datetime", AdvancingClock)

    with pytest.raises(ReleaseRejectedError, match="expired during verification"):
        prepare_operator_input(home / "run/operator.json")


def test_unknown_prepared_flag_is_not_silently_forwarded() -> None:
    with pytest.raises(SystemExit) as error:
        main.main(["cluster", "update", "--local", "--prepared", "/x", "--permit-ready"])
    assert error.value.code == 2


def test_preparation_error_returns_refusal_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_path: Path) -> PreparedOperatorInput:
        raise ReleaseRejectedError("unknown LKG")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", refuse)
    args = main._build_parser().parse_args(["cluster", "update", "--local", "--prepared", "/x"])
    assert run_prepared_update(args) == 2


def test_validated_input_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Path] = []

    def verified(path: Path) -> PreparedOperatorInput:
        seen.append(path)
        return cast("PreparedOperatorInput", object())

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", verified)
    args = main._build_parser().parse_args(["cluster", "update", "--local", "--prepared", "/x"])

    assert run_prepared_update(args) == 2
    assert seen == [Path("/x")]
    assert "no operation was started" in capsys.readouterr().err


def test_actual_parser_refuses_without_importing_settings_or_commands(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'shared.config', 'cli.commands'}:
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
from cli.main import main
assert main(['cluster', 'update', '--prepared', '/absent']) == 2
assert main(['cluster', 'update', '--local', '--prepar', '/absent']) == 2
"""
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "AVA_HOME": str(tmp_path / "unit"),
        "AVA_CLI_LOG_NAME": "prepared-import-guard",
    }
    result = subprocess.run(  # noqa: S603 — fixed interpreter and literal guard program.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_image_keeps_full_receipt_and_writer_inventory_digests_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path.resolve()
    (home / "machine_name").write_text("gateway\n", encoding="utf-8")
    (home / "run").mkdir()
    expected = ExpectedUnitWriters(
        machine="gateway",
        home=str(home),
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
        processes=(),
        sessions=(),
        launchers=(),
    )
    receipt = PreparationReceipt(
        version=1,
        expected=expected,
        services=(PreparedService(session="ava-ops", requires_db=True, gate=None),),
        inventory_digest=expected.unit().inventory_digest,
        closure="unknown",
        unresolved=("writer closure",),
    )
    body = receipt.model_dump_json().encode()
    prepared_receipt_digest = hashlib.sha256(body).hexdigest()
    (home / "run" / f"release-inventory-{prepared_receipt_digest}.json").write_bytes(body)
    unit = PublishedUnit(
        machine="gateway",
        home=str(home),
        inventory_digest=expected.unit().inventory_digest,
        prepared_receipt_digest=prepared_receipt_digest,
        artifact_digest=expected.artifact_digest,
        manifest_digest=expected.manifest_digest,
    )
    release_root = home / "releases" / unit.artifact_digest

    def verified_release(_store: Path, _digest: str, **_kwargs: object) -> VerifiedRelease:
        return VerifiedRelease(
            digest=unit.artifact_digest,
            manifest_digest=unit.manifest_digest,
            root=release_root,
            interpreter=release_root / "venv/bin/python",
            cwd=release_root,
        )

    monkeypatch.setattr("cli.prepared_update.verify_release", verified_release)

    _, loaded_receipt = _image(unit, "d" * 64)
    assert loaded_receipt == receipt
    with pytest.raises(ReleaseRejectedError, match="another unit/image"):
        _image(unit.model_copy(update={"inventory_digest": "7" * 64}), "d" * 64)
