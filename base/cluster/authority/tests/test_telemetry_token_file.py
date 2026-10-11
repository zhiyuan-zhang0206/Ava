"""The telemetry token a gateway home's start publishes for the services that must not hold the
human cluster secret (the heartbeat's observability-station probe)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from base.cluster.authority.api import (
    publish_telemetry_token,
    read_telemetry_token,
    telemetry_token,
    telemetry_token_path,
)


def test_a_published_token_is_the_derivation_and_owner_only(tmp_path: Path) -> None:
    publish_telemetry_token(tmp_path, "the-secret")

    assert read_telemetry_token(tmp_path) == telemetry_token("the-secret")
    assert stat.S_IMODE(telemetry_token_path(tmp_path).stat().st_mode) == 0o600
    assert "the-secret" not in telemetry_token_path(tmp_path).read_text()


def test_a_rotated_secret_replaces_the_token(tmp_path: Path) -> None:
    publish_telemetry_token(tmp_path, "old")
    publish_telemetry_token(tmp_path, "new")

    assert read_telemetry_token(tmp_path) == telemetry_token("new")


def test_a_blank_secret_publishes_no_token_and_keeps_the_published_one(tmp_path: Path) -> None:
    publish_telemetry_token(tmp_path, "the-secret")
    with pytest.raises(ValueError, match="non-empty cluster secret"):
        publish_telemetry_token(tmp_path, "")

    assert read_telemetry_token(tmp_path) == telemetry_token("the-secret")


def test_a_home_that_never_published_reads_none(tmp_path: Path) -> None:
    assert read_telemetry_token(tmp_path) is None
