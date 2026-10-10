"""The WAL-G configuration: strict, secret-free in every message, and the key pinned on first use."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.config import settings
from services.backup.walg import config as walg_config
from services.backup.walg.tests.support import (
    ACCESS_KEY_ID,
    ACCESS_KEY_SECRET,
    KEY_HEX,
    SECRETS,
    Sandbox,
    make_sandbox,
    valid_config,
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _refused(sandbox: Sandbox, match: str) -> str:
    with pytest.raises(walg_config.WalgConfigError, match=match) as caught:
        walg_config.read_config(sandbox.config_file)
    return str(caught.value)


def test_unset_means_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    assert walg_config.configured_path(path_reader=lambda: settings.walg.walg_config_file) is None
    assert walg_config.enabled(path_reader=lambda: settings.walg.walg_config_file) is False
    with pytest.raises(walg_config.WalgConfigError, match="AVA_WALG_CONFIG_FILE is not set"):
        walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)


def test_a_valid_configuration_loads_and_exposes_only_non_secret_facts(sandbox: Sandbox) -> None:
    config = walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)

    assert config.path == sandbox.config_file
    assert config.prefix == "oss://ava-backups/ava-walg/test-home/pg17/gen1/"
    assert config.key_path == sandbox.key_file
    assert len(config.key_fingerprint) == 16
    assert not any(secret in repr(config) for secret in SECRETS)


@pytest.mark.parametrize(
    "missing",
    [
        "WALG_OSS_PREFIX",
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_ENDPOINT",
        "OSS_REGION",
        "WALG_LIBSODIUM_KEY_PATH",
    ],
)
def test_every_required_setting_must_be_present(sandbox: Sandbox, missing: str) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, **{missing: None}))

    assert missing in _refused(sandbox, "must set")


def test_the_region_is_the_bare_id_not_the_endpoint_form(sandbox: Sandbox) -> None:
    """OSS signs with `cn-shanghai`; `oss-cn-shanghai` fails with "Invalid signing region"."""
    sandbox.write_config(valid_config(sandbox.key_file, OSS_REGION="oss-cn-shanghai"))

    message = _refused(sandbox, "OSS_REGION")

    assert "cn-shanghai, not oss-cn-shanghai" in message


def test_the_key_transform_must_be_hex(sandbox: Sandbox) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_LIBSODIUM_KEY_TRANSFORM="base64"))

    _refused(sandbox, "WALG_LIBSODIUM_KEY_TRANSFORM to hex")


@pytest.mark.parametrize("value", [None, "false", False, "yes"])
def test_wal_overwrite_prevention_is_required(sandbox: Sandbox, value: object) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_PREVENT_WAL_OVERWRITE=value))

    _refused(sandbox, "WALG_PREVENT_WAL_OVERWRITE to true")


@pytest.mark.parametrize("value", [True, "true", "TRUE"])
def test_wal_overwrite_prevention_accepts_what_wal_g_reads_as_true(
    sandbox: Sandbox, value: object
) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_PREVENT_WAL_OVERWRITE=value))

    assert walg_config.read_config(sandbox.config_file).prefix


@pytest.mark.parametrize(
    ("prefix", "why"),
    [
        ("s3://bucket/ava-walg/", "oss://<bucket>/<path>"),
        ("oss://bucket", "bucket root"),
        ("oss://bucket/", "bucket root"),
        ("oss://bucket/ava-logical/x/", "ava-logical/"),
        ("oss://bucket/ava-pitr-scratch/walg/", "ava-pitr-scratch/"),
        ("oss://bucket/ava-wsl-cutover-20260901/", "ava-wsl-cutover-20260901/"),
    ],
)
def test_the_prefix_cannot_overlap_another_backup_namespace(
    sandbox: Sandbox, prefix: str, why: str
) -> None:
    sandbox.write_config(valid_config(sandbox.key_file, WALG_OSS_PREFIX=prefix))

    assert why in _refused(sandbox, "WALG_OSS_PREFIX")


def test_the_config_file_must_be_owner_only(sandbox: Sandbox) -> None:
    sandbox.config_file.chmod(0o644)

    assert "owner-only" in _refused(sandbox, "WAL-G config")


def test_a_symlinked_config_file_is_refused(sandbox: Sandbox, tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(sandbox.config_file.read_text())
    real.chmod(0o600)
    sandbox.config_file.unlink()
    sandbox.config_file.symlink_to(real)

    assert "symlink" in _refused(sandbox, "WAL-G config")


def test_a_missing_or_malformed_config_is_refused(sandbox: Sandbox) -> None:
    sandbox.config_file.write_text("{not json")
    _refused(sandbox, "not readable JSON")

    sandbox.config_file.write_text(json.dumps(["a", "list"]))
    _refused(sandbox, "must be a JSON object")

    sandbox.config_file.unlink()
    _refused(sandbox, "does not exist")


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("ab" * 31, "64 lowercase hex"),
        ("AB" * 32, "64 lowercase hex"),
        ("zz" * 32, "64 lowercase hex"),
    ],
)
def test_the_key_must_be_32_bytes_of_lowercase_hex(
    sandbox: Sandbox, content: str, why: str
) -> None:
    sandbox.key_file.write_text(content)

    assert why in _refused(sandbox, "encryption key")


def test_the_key_file_must_be_owner_only(sandbox: Sandbox) -> None:
    sandbox.key_file.chmod(0o640)

    assert "owner-only" in _refused(sandbox, "encryption key")


def test_no_refusal_ever_contains_a_secret(sandbox: Sandbox) -> None:
    messages: list[str] = []
    for overrides in (
        {"WALG_LIBSODIUM_KEY_TRANSFORM": "base64"},
        {"WALG_OSS_PREFIX": "oss://bucket/ava-logical/"},
        {"WALG_PREVENT_WAL_OVERWRITE": "false"},
        {"OSS_REGION": None},
    ):
        sandbox.write_config(valid_config(sandbox.key_file, **overrides))
        messages.append(_refused(sandbox, "WAL-G config"))
    sandbox.config_file.write_text("{" + ACCESS_KEY_ID + ACCESS_KEY_SECRET)
    messages.append(_refused(sandbox, "WAL-G config"))
    sandbox.write_config(valid_config(sandbox.key_file))
    sandbox.key_file.write_text("Z" + KEY_HEX)
    messages.append(_refused(sandbox, "encryption key"))

    assert len(messages) == 6
    assert not any(secret in message for message in messages for secret in SECRETS)


# ── the key fingerprint pin (trust on first use) ─────────────────────────────


def test_the_first_load_pins_the_key_fingerprint(sandbox: Sandbox) -> None:
    assert walg_config.pinned_key_id() is None

    config = walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)

    pin = walg_config.key_id_path()
    assert pin == sandbox.home / "backups" / "walg" / "key-id"
    assert pin.read_text().strip() == config.key_fingerprint
    assert pin.stat().st_mode & 0o777 == 0o600
    assert walg_config.pinned_key_id() == config.key_fingerprint
    assert KEY_HEX not in pin.read_text()


def test_a_second_load_with_the_same_key_is_idempotent(sandbox: Sandbox) -> None:
    first = walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)

    assert walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file) == first


def test_a_different_key_is_refused_and_the_pin_is_kept(sandbox: Sandbox) -> None:
    pinned = walg_config.load_walg_config(
        path_reader=lambda: settings.walg.walg_config_file
    ).key_fingerprint
    sandbox.key_file.write_text("cd" * 32 + "\n")

    with pytest.raises(
        walg_config.WalgConfigError, match="is not the key this home pinned"
    ) as caught:
        walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)

    assert pinned in str(caught.value)
    assert "cd" * 32 not in str(caught.value)
    assert walg_config.pinned_key_id() == pinned
    problem = walg_config.pin_problem(walg_config.read_config(sandbox.config_file))
    assert problem is not None and "is not the key this home pinned" in problem


def test_reading_never_pins(sandbox: Sandbox) -> None:
    walg_config.read_config(sandbox.config_file)

    assert walg_config.pinned_key_id() is None
    assert walg_config.pin_problem(walg_config.read_config(sandbox.config_file)) is None


def test_a_corrupt_pin_is_an_error_not_a_reset(sandbox: Sandbox) -> None:
    walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)
    walg_config.key_id_path().write_text("not-a-fingerprint\n")

    with pytest.raises(walg_config.WalgConfigError, match="malformed"):
        walg_config.load_walg_config(path_reader=lambda: settings.walg.walg_config_file)
