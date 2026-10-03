"""An unreadable process-group member is not an empty execution domain."""

from collections.abc import Iterator
from unittest.mock import Mock

import psutil
import pytest


@pytest.mark.parametrize("failure", [PermissionError, psutil.AccessDenied])
def test_unreadable_group_member_is_not_an_empty_domain(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    from base.native_process.exec_domain import _process_group_has_live_member

    process = Mock(info={"pid": 123, "status": psutil.STATUS_RUNNING})

    def iter_processes(_attrs: list[str]) -> Iterator[Mock]:
        return iter([process])

    monkeypatch.setattr(psutil, "process_iter", iter_processes)

    def unreadable(pid: int) -> int:
        raise failure()

    monkeypatch.setattr("base.native_process.exec_domain.os.getpgid", unreadable)
    with pytest.raises(failure):
        _process_group_has_live_member(123)
