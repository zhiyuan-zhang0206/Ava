"""services/gateway_side/backup/offsite.py: the OSS publish of a finished daily dump.

The bucket is an in-memory fake at the `OssBucket` seam (`oss_fake.py`). The
publisher's contract: iff-absent through forbid-overwrite on Complete only,
adopt-after-crash when the occupied name provably holds the same content,
refusal of any identity mismatch, a silent skip when nothing is configured,
and a visible ACK line (success judged by destination state, #2620).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

from base.config import settings
from services.gateway_side.backup import offsite
from services.gateway_side.backup.names import REMOTE_ROOT
from services.gateway_side.backup.tests.oss_fake import FakeOssBucket, chain_etag, md5_hex

_REPO = Path(__file__).resolve().parents[4]
_NAME = "ava-20260916T030000Z.dump.enc"
_METADATA = {"ava-artifact-kind": "logical-backup"}


def _artifact(tmp_path: Path, data: bytes = b"encrypted artifact") -> Path:
    path = tmp_path / _NAME
    path.write_bytes(data)
    return path


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_store_backend", "oss")
    monkeypatch.setattr(config, "pitr_oss_endpoint", "https://oss-cn-test.example.com/")
    monkeypatch.setattr(config, "pitr_oss_bucket", "backups")
    monkeypatch.setattr(config, "pitr_oss_credentials_file", tmp_path / "oss.json")


# ── the upload itself ──


def test_publish_streams_a_multipart_object_and_verifies_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact, bucket = _artifact(tmp_path), FakeOssBucket()
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    published = offsite.publish(artifact, bucket=bucket)

    assert published == f"{REMOTE_ROOT}/{_NAME}"
    stored = bucket.files[f"{REMOTE_ROOT}/{_NAME}"]
    assert stored["data"] == b"encrypted artifact"
    assert stored["headers"] == {"x-oss-meta-ava-artifact-kind": "logical-backup"}
    assert stored["etag"] == chain_etag([b"encrypted artifact"])
    assert bucket.aborted == []
    [ack_line] = [r.getMessage() for r in caplog.records if "off-site published" in r.getMessage()]
    assert f"{published} (size={len(b'encrypted artifact')}" in ack_line
    assert f"pin={stored['etag']}" in ack_line
    assert f"checksum=md5:{md5_hex(b'encrypted artifact')}" in ack_line
    assert artifact.read_bytes() == b"encrypted artifact"


def test_forbid_overwrite_rides_only_on_complete(tmp_path: Path) -> None:
    """On init it would fail an occupied name at once and kill adopt-after-crash."""
    bucket = FakeOssBucket()

    offsite.publish(_artifact(tmp_path), bucket=bucket)

    headers = dict(bucket.calls)
    assert "x-oss-forbid-overwrite" not in headers["init"]
    assert "x-oss-forbid-overwrite" not in headers["part"]
    assert headers["complete"] == {"x-oss-forbid-overwrite": "true"}
    assert headers["part"]["Content-MD5"]


def test_a_large_artifact_is_split_into_chained_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(offsite, "PART_SIZE", 5)
    payload = b"0123456789abcdefg"  # 5 + 5 + 5 + 2
    bucket = FakeOssBucket()

    ack = offsite.put_if_absent(bucket, _artifact(tmp_path, payload), "x/object", _METADATA)

    assert ack.created and ack.size == len(payload)
    assert ack.pin_token == chain_etag([payload[0:5], payload[5:10], payload[10:15], payload[15:]])
    assert ack.pin_token.endswith("-4")
    assert ack.md5 == md5_hex(payload)
    assert bucket.files["x/object"]["data"] == payload


# ── adopt-after-crash and identity refusal ──


def test_adopt_after_crash_returns_the_existing_object(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A crashed or concurrent run left the canonical name complete; the retry's
    parts hash to the same chain, so it adopts the object and aborts its upload."""
    artifact, bucket = _artifact(tmp_path), FakeOssBucket()
    first = offsite.put_if_absent(bucket, artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA)
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    second = offsite.put_if_absent(bucket, artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA)

    assert first.created and not second.created
    assert second.pin_token == first.pin_token and second.md5 == first.md5
    assert bucket.aborted == ["up2"], "the losing upload must not linger as a fragment"
    assert offsite.publish(artifact, bucket=bucket) == f"{REMOTE_ROOT}/{_NAME}"
    assert any("off-site published" in r.getMessage() for r in caplog.records)


def test_an_occupied_name_with_a_different_etag_chain_is_not_adopted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bucket = FakeOssBucket()
    # Same size and metadata as the artifact, so only the ETag chain can tell them apart.
    bucket.seed(f"{REMOTE_ROOT}/{_NAME}", [b"eNcrypted artifact"], _METADATA)
    artifact = _artifact(tmp_path)

    with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
        offsite.put_if_absent(bucket, artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA)

    assert bucket.files[f"{REMOTE_ROOT}/{_NAME}"]["data"] == b"eNcrypted artifact"
    caplog.set_level(logging.INFO, logger=offsite.__name__)
    assert offsite.publish(artifact, bucket=bucket) is None
    assert any(
        "off-site publish of" in r.getMessage() and "failed" in r.getMessage()
        for r in caplog.records
    )
    assert artifact.read_bytes() == b"encrypted artifact"


def test_an_occupied_name_with_different_metadata_is_not_adopted(tmp_path: Path) -> None:
    bucket = FakeOssBucket()
    bucket.seed(f"{REMOTE_ROOT}/{_NAME}", [b"encrypted artifact"], {"ava-artifact-kind": "other"})

    with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
        offsite.put_if_absent(bucket, _artifact(tmp_path), f"{REMOTE_ROOT}/{_NAME}", _METADATA)


def test_a_part_etag_that_is_not_its_content_md5_aborts_the_upload(tmp_path: Path) -> None:
    bucket = FakeOssBucket()
    bucket.corrupt_part_etags = True

    with pytest.raises(offsite.OffsiteError, match="part ETag does not match"):
        offsite.put_if_absent(bucket, _artifact(tmp_path), "x/object", _METADATA)

    assert "x/object" not in bucket.files
    assert bucket.aborted == ["up1"]


def test_a_completed_etag_that_breaks_the_part_chain_is_refused(tmp_path: Path) -> None:
    bucket = FakeOssBucket()
    bucket.corrupt_complete_etag = True

    with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
        offsite.put_if_absent(bucket, _artifact(tmp_path), "x/object", _METADATA)


def test_an_empty_artifact_is_refused_before_any_request(tmp_path: Path) -> None:
    bucket = FakeOssBucket()

    with pytest.raises(offsite.OffsiteError, match="non-empty"):
        offsite.put_if_absent(bucket, _artifact(tmp_path, b""), "x/object", _METADATA)

    assert bucket.calls == []


def test_a_rejected_init_keeps_the_local_artifact_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bucket = FakeOssBucket()
    bucket.fail_init = (403, "AccessDenied")
    artifact = _artifact(tmp_path)

    assert offsite.publish(artifact, bucket=bucket) is None

    assert artifact.read_bytes() == b"encrypted artifact"
    assert any(r.exc_info is not None for r in caplog.records), "the failure must carry its cause"


def test_the_root_is_a_parameter(tmp_path: Path) -> None:
    """A smoke run publishes under a scratch prefix and never touches `ava-logical/`."""
    bucket = FakeOssBucket()

    published = offsite.publish(_artifact(tmp_path), root="ava-pitr-scratch/t1", bucket=bucket)

    assert published == f"ava-pitr-scratch/t1/{_NAME}"
    assert list(bucket.files) == [published]


# ── configuration ──


def test_unconfigured_publish_skips_with_one_info_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Not configured is a supported state: no ERROR, no traceback, no store opened."""
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "gcs")

    def _never_opened(_target: object) -> None:
        raise AssertionError("an unconfigured destination must never be opened")

    monkeypatch.setattr(offsite, "open_bucket", _never_opened)
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    assert offsite.publish(_artifact(tmp_path)) is None

    assert [(r.levelno, r.exc_info) for r in caplog.records] == [(logging.INFO, None)]
    assert "AVA_PITR_STORE_BACKEND=gcs" in caplog.records[0].getMessage()


def test_oss_with_unset_keys_skips_naming_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings.physical_backup, "pitr_oss_bucket", "")
    monkeypatch.setattr(settings.physical_backup, "pitr_oss_credentials_file", None)
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    assert offsite.publish(_artifact(tmp_path)) is None

    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert "AVA_PITR_OSS_BUCKET" in record.getMessage()
    assert "AVA_PITR_OSS_CREDENTIALS_FILE" in record.getMessage()
    assert "AVA_PITR_OSS_ENDPOINT" not in record.getMessage()


def test_a_configured_destination_publishes_to_the_opened_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, tmp_path)
    bucket = FakeOssBucket()
    opened: list[offsite.OssTarget] = []

    def _open(target: offsite.OssTarget) -> FakeOssBucket:
        opened.append(target)
        return bucket

    monkeypatch.setattr(offsite, "open_bucket", _open)

    published = offsite.publish(_artifact(tmp_path))

    assert published == f"{REMOTE_ROOT}/{_NAME}" and published in bucket.files
    assert opened == [
        offsite.OssTarget("https://oss-cn-test.example.com/", "backups", tmp_path / "oss.json")
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'["a"]',
        b'{"access_key_id": "id"}',
        b'{"access_key_id": "", "access_key_secret": "s"}',
    ],
)
def test_an_unusable_credentials_file_keeps_the_local_artifact(
    payload: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure(monkeypatch, tmp_path)
    (tmp_path / "oss.json").write_bytes(payload)
    artifact = _artifact(tmp_path)

    assert offsite.publish(artifact) is None

    assert artifact.read_bytes() == b"encrypted artifact"
    assert any(
        "off-site store unavailable" in r.getMessage() and r.exc_info is not None
        for r in caplog.records
    )


def test_open_bucket_reads_the_access_key_pair(tmp_path: Path) -> None:
    credentials = tmp_path / "oss.json"
    credentials.write_text('{"access_key_id": "id", "access_key_secret": "secret"}')

    bucket = cast(
        Any,
        offsite.open_bucket(
            offsite.OssTarget("https://oss-cn-test.example.com/", "backups", credentials)
        ),
    )

    assert bucket.bucket_name == "backups"
    assert bucket.endpoint == "https://oss-cn-test.example.com"


# ── the standalone entry: success and failure both leave evidence on stderr ──


def _entry(
    tmp_path: Path, body: str, *args: str, configured: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `python -m services.backup --publish-offsite` logic in a fresh interpreter.

    A fresh interpreter is deliberate: pytest's own root handler would mask the
    standalone logging behavior the entry point configures. `configured` gives
    it an OSS destination (the settings read is covered in-process above)."""
    code = textwrap.dedent(f"""
        import sys

        sys.path.insert(0, {str(_REPO)!r})
        from services.backup import _main
        from services.gateway_side.backup import offsite
        from services.gateway_side.backup.tests.oss_fake import FakeOssBucket

        if {configured!r}:
            offsite.configured_target = lambda: offsite.OssTarget("https://oss.example", "b", None)
        {textwrap.indent(textwrap.dedent(body), "        ").lstrip()}
        raise SystemExit(_main({["--publish-offsite", str(tmp_path / _NAME), *args]!r}))
    """)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )


def test_standalone_success_is_visible(tmp_path: Path) -> None:
    """A fully silent success was once read as a dead upload (misdiagnosed 2026-09-16)."""
    _artifact(tmp_path)

    proc = _entry(
        tmp_path,
        """
        offsite.open_bucket = lambda target: FakeOssBucket()
        """,
        "--offsite-root",
        "ava-pitr-scratch/smoke",
    )

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site published" in proc.stderr
    assert f"ava-pitr-scratch/smoke/{_NAME}" in proc.stderr


def test_standalone_unconfigured_still_says_so_and_exits_zero(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    proc = _entry(tmp_path, "", configured=False)

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site publish skipped" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"


def test_standalone_publish_failure_keeps_exit_and_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    proc = _entry(
        tmp_path,
        """
        bucket = FakeOssBucket()
        bucket.fail_init = (500, "InternalError")
        offsite.open_bucket = lambda target: bucket
        """,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"[backup] off-site publish of {REMOTE_ROOT}/{_NAME} failed" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"
