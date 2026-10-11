"""The five crontab jobs carry fixed markers: registering leaves exactly one line
per job, registering again changes nothing, and unregistering removes only the
Ava lines."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from base.host.system import cron, logs_job, packages_job, pr_flow_job, walg_job

_UNRELATED = "0 3 * * * /usr/local/bin/backup"

_MARKERS = (
    "# ava-logs-maintenance",
    "# ava-packages-refresh",
    "# ava-pr-flow",
    "# ava-health-probe",
)
_WALG_MARKER = "# ava-walg"
_REGISTERS = (
    logs_job._register_linux,
    lambda: packages_job._register_linux(tick_reader=lambda: 900),
    pr_flow_job._register_linux,
    lambda: walg_job._register_linux(backup_hour_reader=lambda: 3),
)
_UNREGISTERS = (
    logs_job._unregister_linux,
    packages_job._unregister_linux,
    pr_flow_job._unregister_linux,
    walg_job._unregister_linux,
)


class _Crontab:
    """An in-memory user crontab behind `crontab -l` and `crontab -`."""

    def __init__(self, text: str) -> None:
        self.text = text

    def run(self, argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=self.text, stderr="")
        assert argv == ["crontab", "-"], argv
        self.text = str(kwargs["input"])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@pytest.fixture
def crontab(default_home: Path, monkeypatch: pytest.MonkeyPatch) -> _Crontab:
    fake = _Crontab(_UNRELATED + "\n")
    monkeypatch.setattr(cron.shutil, "which", lambda _name: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cron.subprocess, "run", fake.run)
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/home/operator/.ava/source/.venv/bin/ava")
    return fake


def _register_all() -> None:
    assert cron._register_linux(300) == 0
    for register in _REGISTERS:
        assert register() == 0


def test_register_leaves_exactly_one_line_per_job(crontab: _Crontab) -> None:
    _register_all()

    assert len(crontab.lines) == 6
    assert _UNRELATED in crontab.lines
    for marker in (*_MARKERS, _WALG_MARKER):
        assert len([line for line in crontab.lines if marker in line]) == 1, marker
        assert len([line for line in crontab.lines if line.endswith(marker)]) == 1, marker


def test_registering_again_changes_nothing(crontab: _Crontab) -> None:
    _register_all()
    first = sorted(crontab.lines)
    _register_all()

    assert sorted(crontab.lines) == first


def test_unregister_removes_the_ava_lines_and_only_them(crontab: _Crontab) -> None:
    _register_all()

    assert cron._unregister_linux() == 0
    for unregister in _UNREGISTERS:
        assert unregister() == 0

    assert crontab.lines == [_UNRELATED]
