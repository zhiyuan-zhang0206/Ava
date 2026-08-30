"""SigV4 signer contract: pinned AWS documentation vector + query shape."""

from __future__ import annotations

import hashlib
from pathlib import Path

from services.pitr.cos_client import _signature_v4

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def test_signature_matches_the_aws_documented_s3_vector() -> None:
    """The documented AWS SigV4 S3 GET example (AWS docs, "Signature
    Calculations for the Authorization Header"): an independent golden
    vector proving the signer matches the real AWS4-HMAC-SHA256 math —
    the COS S3-compatible interface requires exactly this signing."""
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": _EMPTY_SHA256,
        "x-amz-date": "20130524T000000Z",
    }
    authorization = _signature_v4(
        method="GET",
        url_path="/test.txt",
        query=None,
        headers=headers,
        payload_hash=_EMPTY_SHA256,
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # noqa: S106 — AWS docs fixture
        region="us-east-1",
        amz_date="20130524T000000Z",
    )
    assert authorization == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/"
        "s3/aws4_request, SignedHeaders=host;range;x-amz-content-sha256;"
        "x-amz-date, Signature="
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_signature_sorts_query_and_keeps_path_segments() -> None:
    """The listing canonical form must sort the query by encoded key and
    keep the object path segments unnormalized."""
    headers = {
        "host": "ava-cos-1250000000.cos.ap-shanghai.myqcloud.com",
        "x-amz-content-sha256": _EMPTY_SHA256,
        "x-amz-date": "20130524T000000Z",
    }
    first = _signature_v4(
        method="GET",
        url_path="/ava-pitr/wal/00000001/000000010000000000000001.enc",
        query={"list-type": "2", "prefix": "ava-pitr/"},
        headers=headers,
        payload_hash=_EMPTY_SHA256,
        access_key_id="AK",
        secret_access_key="SK",  # noqa: S106 — fixture identity
        region="ap-shanghai",
        amz_date="20130524T000000Z",
    )
    reordered = _signature_v4(
        method="GET",
        url_path="/ava-pitr/wal/00000001/000000010000000000000001.enc",
        query={"prefix": "ava-pitr/", "list-type": "2"},
        headers=headers,
        payload_hash=_EMPTY_SHA256,
        access_key_id="AK",
        secret_access_key="SK",  # noqa: S106 — fixture identity
        region="ap-shanghai",
        amz_date="20130524T000000Z",
    )
    assert first == reordered
    assert "Credential=AK/20130524/ap-shanghai/s3/aws4_request" in first


def test_credential_evidence_rejects_overexposed_file(tmp_path: Path) -> None:
    import json

    import pytest

    from services.pitr.cos_client import CosPermanentError, credential_evidence

    path = tmp_path / "cos.json"
    path.write_text(json.dumps({"secret_id": "AKIDx", "secret_key": "s"}))
    path.chmod(0o644)
    with pytest.raises(CosPermanentError, match="unsafe"):
        credential_evidence(path, region="ap-guangzhou", bucket="ava-pitr-1250000000")


def test_credential_evidence_reports_cos_identity(tmp_path: Path) -> None:
    import json

    from services.pitr.cos_client import credential_evidence

    path = tmp_path / "cos.json"
    path.write_text(json.dumps({"secret_id": "AKIDx", "secret_key": "s"}))
    path.chmod(0o600)
    evidence = credential_evidence(path, region="ap-guangzhou", bucket="ava-pitr-1250000000")
    assert evidence == {
        "backend": "cos",
        "uploader_identity": "AKIDx",
        "viewer_identity": "AKIDx",
        "store_target": "ava-pitr-1250000000",
    }
