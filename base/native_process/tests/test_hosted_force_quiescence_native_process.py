from collections.abc import Iterator
from unittest.mock import Mock

import psutil
import pytest

from base.native_process.exec_domain import process_group_has_live_member


class _UnreadableProcessControl:
    def __init__(self, failure: type[Exception]) -> None:
        self._failure = failure

    def kill(self, pid: int, signum: int, /) -> None:
        raise AssertionError((pid, signum))

    def killpg(self, pgid: int, signum: int, /) -> None:
        raise AssertionError((pgid, signum))

    def getpgid(self, pid: int, /) -> int:
        raise self._failure()

    def getsid(self, pid: int, /) -> int:
        raise AssertionError(pid)


@pytest.mark.parametrize("failure", [PermissionError, psutil.AccessDenied])
def test_unreadable_group_member_is_not_an_empty_domain(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    process = Mock(info={"pid": 123, "status": psutil.STATUS_RUNNING})

    def iter_processes(_attrs: list[str]) -> Iterator[Mock]:
        return iter([process])

    monkeypatch.setattr(psutil, "process_iter", iter_processes)
    with pytest.raises(failure):
        process_group_has_live_member(123, process_control=_UnreadableProcessControl(failure))
