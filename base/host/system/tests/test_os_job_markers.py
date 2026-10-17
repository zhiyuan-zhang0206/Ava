from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system import cron, logs_job, packages_job, pr_flow_job, walg_job
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import LINUX_X86, fake_host
from base.host.system.tests.fake_crontab import FakeCrontab

_UNRELATED = "0 3 * * * /usr/local/bin/backup"

_MARKERS = (
    "# meiva-logs-maintenance",
    "# meiva-packages-refresh",
    "# meiva-pr-flow",
    "# meiva-health-probe",
)
_WALG_MARKER = "# meiva-walg"
_OPERATOR_MEIVA = "/home/operator/.meiva/source/.venv/bin/meiva"


def _operator_meiva(*, host: Host) -> str:
    return _OPERATOR_MEIVA


@pytest.fixture
def crontab(default_home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    monkeypatch.setattr(cron, "meiva_binary_path", _operator_meiva)
    return FakeCrontab(_UNRELATED + "\n")


def _linux(crontab: FakeCrontab) -> Host:
    return fake_host(crontab, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})


def _register_all(crontab: FakeCrontab) -> None:
    host = _linux(crontab)
    assert cron.register_linux(300, host=host) == 0
    assert logs_job.register_linux(host=host) == 0
    assert packages_job.register_linux(tick_reader=lambda: 900, host=host) == 0
    assert pr_flow_job.register_linux(host=host) == 0
    assert walg_job.register_linux(backup_hour_reader=lambda: 3, host=host) == 0


def test_register_leaves_exactly_one_line_per_job(crontab: FakeCrontab) -> None:
    _register_all(crontab)

    assert len(crontab.lines) == 6
    assert _UNRELATED in crontab.lines
    for marker in (*_MARKERS, _WALG_MARKER):
        assert len([line for line in crontab.lines if marker in line]) == 1, marker
        assert len([line for line in crontab.lines if line.endswith(marker)]) == 1, marker


def test_registering_again_changes_nothing(crontab: FakeCrontab) -> None:
    _register_all(crontab)
    first = sorted(crontab.lines)
    _register_all(crontab)

    assert sorted(crontab.lines) == first


def test_unregister_removes_the_meiva_lines_and_only_them(crontab: FakeCrontab) -> None:
    _register_all(crontab)

    host = _linux(crontab)
    assert cron.unregister_linux(host=host) == 0
    for unregister in (
        logs_job.unregister_linux,
        packages_job.unregister_linux,
        pr_flow_job.unregister_linux,
        walg_job.unregister_linux,
    ):
        assert unregister(host=host) == 0

    assert crontab.lines == [_UNRELATED]
