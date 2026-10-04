"""Service custody records: a pending birth, a mutated record and an invalid native birth are refused."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.supervision.ava_root.custody import ServiceCustody, require_clear


def test_pending_birth_survives_process_owner_loss(tmp_path: Path) -> None:
    ServiceCustody(tmp_path, "gateway")
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        require_clear(tmp_path)
    with pytest.raises(FileExistsError):
        ServiceCustody(tmp_path, "gateway")


def test_mutated_custody_is_never_overwritten_or_cleared(tmp_path: Path) -> None:
    record = ServiceCustody(tmp_path, "worker")
    record.path.write_text("replacement authority")
    for operation in (lambda: record.retain(set(), group=os.getpid()), record.clear):
        with pytest.raises(RuntimeError, match="custody changed"):
            operation()
        assert record.path.read_text() == "replacement authority"


@pytest.mark.parametrize("birth", [float("nan"), float("inf"), -1, True])
def test_native_status_refuses_invalid_birth(birth: object) -> None:
    from base.native_process.root_control.client import RootClientError, native_identity

    with pytest.raises(RootClientError, match="captured native birth"):
        native_identity({"pid": 101, "create_time": birth, "starttime": None})
