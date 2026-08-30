"""Baidu adapter role tests: token manager, pinned restore download,
retention inventory, and the protected-manifest publisher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.pitr.baidu_inventory import BaiduRetentionInventoryReader
from services.pitr.baidu_publish_store import BaiduProtectedManifestPublisher
from services.pitr.baidu_token import BaiduCredentials, BaiduTokenError, BaiduTokenManager
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import PermanentObjectStoreError
from services.pitr.restore_manifest import RestoreObject
from services.pitr.token_manager import read_token_state
from tests.services.baidu_test_support import (
    APP_ROOT,
    OBJECT,
    FakePcs,
    FakeTokenManager,
    FakeTokenResponse,
    _md5,
    credentials_file,
    make_reader,
    pcs_client_for,
    seed_restore_object,
)

# ── token manager ──


def test_token_manager_refreshes_persists_and_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "token.json"
    posts: list[dict[str, str]] = []

    def fake_post(
        url: str, *, data: dict[str, str], timeout: float, headers: dict[str, str]
    ) -> FakeTokenResponse:
        posts.append(dict(data))
        return FakeTokenResponse(
            200,
            {
                "access_token": "new-access",
                "expires_in": 2592000,
                "refresh_token": "refresh-2",
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    manager = BaiduTokenManager(BaiduCredentials(credentials_file(tmp_path)), state_path)

    assert manager.get_access_token() == "new-access"
    assert posts == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": "app",
            "client_secret": "secret",
        }
    ]
    persisted = read_token_state(state_path)
    assert persisted.access_token == "new-access"  # noqa: S105 — fixture token
    assert persisted.refresh_token == "refresh-2"  # noqa: S105 — fixture token
    assert state_path.stat().st_mode & 0o777 == 0o600
    # The fresh pair outlives the preemptive margin: no second refresh.
    assert manager.get_access_token() == "new-access"
    assert len(posts) == 1


def test_token_refresh_failure_raises_and_health_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_post(
        url: str, *, data: dict[str, str], timeout: float, headers: dict[str, str]
    ) -> FakeTokenResponse:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    manager = BaiduTokenManager(
        BaiduCredentials(credentials_file(tmp_path)), tmp_path / "token.json"
    )

    with pytest.raises(BaiduTokenError, match="transport"):
        manager.get_access_token()

    health = manager.health()
    assert health.refresh_error is not None and "boom" in health.refresh_error


def test_token_manager_rejects_incomplete_credentials(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"app_key": "app"}))
    with pytest.raises(BaiduTokenError, match="secret_key"):
        BaiduCredentials(path)


# ── restore role ──


def test_download_exact_streams_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    reader = make_reader(fake, monkeypatch)
    payload = b"restore-payload" * 8
    row, sidecar_data = seed_restore_object(fake, payload=payload, metadata={"ava-key-id": "v1"})

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        content = payload if url.startswith("https://dl.test/obj") else sidecar_data
        return httpx.Response(200, content=content)

    monkeypatch.setattr(httpx, "get", fake_get)
    expected = RestoreObject(
        "000000010000000000000001",
        OBJECT,
        f"{row['fs_id']}:{_md5(payload)}",
        len(payload),
        MD5,
        _md5(payload),
        (("ava-key-id", "v1"),),
    )
    destination = tmp_path / "out.enc"

    reader.download_exact(expected, destination)

    assert destination.read_bytes() == payload


def test_download_exact_rejects_tampered_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    reader = make_reader(fake, monkeypatch)
    payload = b"restore-payload" * 8
    row, sidecar_data = seed_restore_object(fake, payload=payload, metadata={"ava-key-id": "v1"})
    tampered = payload[:-1] + bytes([payload[-1] ^ 0xFF])

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        content = tampered if url.startswith("https://dl.test/obj") else sidecar_data
        return httpx.Response(200, content=content)

    monkeypatch.setattr(httpx, "get", fake_get)
    expected = RestoreObject(
        "000000010000000000000001",
        OBJECT,
        f"{row['fs_id']}:{_md5(payload)}",
        len(payload),
        MD5,
        _md5(payload),
        (("ava-key-id", "v1"),),
    )
    destination = tmp_path / "out.enc"

    with pytest.raises(PermanentObjectStoreError, match="content differs"):
        reader.download_exact(expected, destination)

    assert not destination.exists()
    assert not (tmp_path / ".out.enc.partial").exists()


# ── inventory role ──


def test_inventory_resolves_sidecars_and_flags_unknowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePcs()
    reader = BaiduRetentionInventoryReader(
        app_root=APP_ROOT, prefix="ava-pitr", token_manager=FakeTokenManager()
    )
    monkeypatch.setattr(
        reader._store,
        "_client",
        lambda: pcs_client_for(fake),
    )

    archive = "000000010000000000000001"
    rel = f"ava-pitr/wal/{archive[:8]}/{archive}.enc"
    wal_sidecar = {
        "object_name": rel,
        "pin_token": "1:m",
        "size": 10,
        "checksum_algo": "md5",
        "checksum_value": "m",
        "metadata": {"ava-archive-name": archive},
    }
    wal_data = json.dumps(wal_sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(f"{APP_ROOT}/{rel}", size=10, md5="m")
    fake.seed_file(
        f"{APP_ROOT}/{rel}.ack.json",
        size=len(wal_data),
        md5=_md5(wal_data),
        dlink="https://dl.test/walside",
    )
    base_rel = "ava-pitr/base/20260830T043835Z/" + "a" * 64 + "/base.tar.zst.enc"
    base_sidecar: dict[str, Any] = {
        "object_name": base_rel,
        "pin_token": "2:m",
        "size": 10,
        "checksum_algo": "md5",
        "checksum_value": "m",
        "metadata": {},
    }
    base_data = json.dumps(base_sidecar, sort_keys=True, separators=(",", ":")).encode()
    fake.seed_file(f"{APP_ROOT}/{base_rel}", size=10, md5="m")
    fake.seed_file(
        f"{APP_ROOT}/{base_rel}.ack.json",
        size=len(base_data),
        md5=_md5(base_data),
        dlink="https://dl.test/baseside",
    )
    orphan = "ava-pitr/wal/00000001/000000010000000000000002.enc"
    fake.seed_file(f"{APP_ROOT}/{orphan}", size=10, md5="m")
    protected = "ava-pitr/protected/some-chain.json"
    fake.seed_file(f"{APP_ROOT}/{protected}", size=10, md5="m")

    sidecar_bodies = {
        "https://dl.test/walside": wal_data,
        "https://dl.test/baseside": base_data,
    }

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, content=sidecar_bodies[url.split("&", 1)[0]])

    monkeypatch.setattr(httpx, "get", fake_get)

    snapshot = reader.snapshot()

    assert snapshot.unknown_names == (orphan,)
    by_kind = {item.kind: item for item in snapshot.objects}
    assert by_kind["wal"].archive_name == archive
    assert by_kind["wal"].pin_token == "1:m"  # noqa: S105 — fixture identity
    assert by_kind["base"].archive_name is None
    assert protected not in snapshot.unknown_names
    assert all(item.object_name != protected for item in snapshot.objects)


# ── publisher role ──


def test_publish_manifest_puts_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePcs()
    publisher = BaiduProtectedManifestPublisher(app_root=APP_ROOT, token_manager=FakeTokenManager())
    monkeypatch.setattr(
        publisher._store,
        "_client",
        lambda: pcs_client_for(fake),
    )
    payload = b'{"protected":true}'

    ack = publisher.put_manifest_if_absent(
        payload=payload,
        object_name="ava-pitr/protected/x.json",
        metadata={"ava-candidate-sha256": "s"},
    )

    assert ack.size == len(payload)
    assert ack.checksum == ObjectChecksum(MD5, _md5(payload))
    assert ack.created is True
    assert fake.calls[0] == "precreate /apps/ava-pitr/ava-pitr/protected/x.json"
