# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""`ava pitr multipart` operator surface (task #3662): `list` renders every
incomplete multipart upload, `abort` previews without --confirm and aborts
exactly one targeted upload with it, and failures exit non-zero with a clean
stderr message instead of a traceback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli.commands import cmd_pitr_multipart_abort, cmd_pitr_multipart_list
from cli.commands import pitr as pitr_commands
from services.pitr import oss_credentials
from services.pitr.oss_multipart import OSSMultipartUploads
from tests.services.oss_test_support import FakeOssBucket


def seed_upload(fake: FakeOssBucket, key: str, *, parts: list[bytes], initiated: int) -> str:
    fake.now = initiated
    init = fake.init_multipart_upload(key)
    upload_id = init.upload_id
    assert upload_id is not None
    for number, payload in enumerate(parts, start=1):
        fake.upload_part(key, upload_id, number, payload)
    return upload_id


def find(fake: FakeOssBucket, key: str, upload_id: str) -> object:
    return OSSMultipartUploads.from_bucket(cast(Any, fake)).find(key=key, upload_id=upload_id)


@pytest.fixture
def opened_calls() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    opened_calls: list[dict[str, Any]],
) -> FakeOssBucket:
    fake = FakeOssBucket()

    def fake_open(**kwargs: Any) -> FakeOssBucket:
        opened_calls.append(kwargs)
        return fake

    monkeypatch.setattr(oss_credentials, "open_oss_bucket", fake_open)
    monkeypatch.setattr(
        pitr_commands,
        "settings",
        SimpleNamespace(
            physical_backup=SimpleNamespace(
                pitr_oss_endpoint="https://oss-cn-shanghai.aliyuncs.com",
                pitr_oss_bucket="ava-pitr-prod",
                pitr_oss_credentials_file=tmp_path / "uploader.json",
            )
        ),
    )
    return fake


def test_list_renders_every_incomplete_upload(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    upload_id = seed_upload(cli_env, "ava-logical/part1.enc", parts=[b"a" * 10], initiated=1)

    assert cmd_pitr_multipart_list(prefix="", credentials_file=None) == 0

    out = capsys.readouterr().out
    assert "ava-logical/part1.enc" in out
    assert f"upload_id={upload_id}" in out
    assert "parts=1" in out
    assert "bytes=10" in out
    assert "initiated=" in out
    assert "age=" in out


def test_list_empty_bucket_is_a_clean_zero(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_pitr_multipart_list(prefix="", credentials_file=None) == 0
    assert "no incomplete multipart uploads" in capsys.readouterr().out


def test_list_prefix_narrows_the_rendering(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_upload(
        cli_env, "ava-wsl-cutover-20260909-d821f0a366/base/x.enc", parts=[b"a"], initiated=1
    )
    seed_upload(cli_env, "ava-logical/keep.enc", parts=[b"b"], initiated=2)

    assert cmd_pitr_multipart_list(prefix="ava-logical/", credentials_file=None) == 0

    out = capsys.readouterr().out
    assert "ava-logical/keep.enc" in out
    assert "ava-wsl-cutover" not in out


def test_list_access_denied_exits_one_with_message(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.list_uploads_error = (403, "AccessDenied")

    assert cmd_pitr_multipart_list(prefix="", credentials_file=None) == 1

    err = capsys.readouterr().err
    assert "pitr multipart list failed" in err
    assert "AccessDenied" in err


def test_abort_preview_does_not_abort(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    upload_id = seed_upload(cli_env, "ava-logical/x.enc", parts=[b"a"], initiated=1)

    assert (
        cmd_pitr_multipart_abort(
            key="ava-logical/x.enc", upload_id=upload_id, credentials_file=None, confirm=False
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "target: key=ava-logical/x.enc" in out
    assert "preview only" in out
    assert find(cli_env, "ava-logical/x.enc", upload_id) is not None


def test_abort_confirm_aborts_exactly_the_target(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    upload_id = seed_upload(cli_env, "ava-logical/x.enc", parts=[b"a"], initiated=1)

    assert (
        cmd_pitr_multipart_abort(
            key="ava-logical/x.enc", upload_id=upload_id, credentials_file=None, confirm=True
        )
        == 0
    )

    assert f"aborted: key=ava-logical/x.enc upload_id={upload_id}" in capsys.readouterr().out
    assert find(cli_env, "ava-logical/x.enc", upload_id) is None


def test_abort_unknown_upload_exits_one(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cmd_pitr_multipart_abort(
            key="missing.enc", upload_id="up404", credentials_file=None, confirm=True
        )
        == 1
    )
    assert "not found" in capsys.readouterr().err


def test_abort_vanished_mid_confirm_exits_one(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    upload_id = seed_upload(cli_env, "ava-logical/race.enc", parts=[b"a"], initiated=1)
    cli_env.abort_error = (404, "NoSuchUpload")

    assert (
        cmd_pitr_multipart_abort(
            key="ava-logical/race.enc", upload_id=upload_id, credentials_file=None, confirm=True
        )
        == 1
    )
    assert "nothing to abort" in capsys.readouterr().err


def test_explicit_credentials_file_wins(
    cli_env: FakeOssBucket, opened_calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    custom = str(tmp_path / "custom-uploader.json")
    assert cmd_pitr_multipart_list(prefix="", credentials_file=custom) == 0
    assert opened_calls[-1]["credentials_file"] == custom


def test_missing_credential_setting_exits_one(
    cli_env: FakeOssBucket, capsys: pytest.CaptureFixture[str]
) -> None:
    pitr_commands.settings.physical_backup.pitr_oss_credentials_file = None

    assert cmd_pitr_multipart_list(prefix="", credentials_file=None) == 1

    assert "AVA_PITR_OSS_CREDENTIALS_FILE" in capsys.readouterr().err
