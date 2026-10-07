"""The five crontab jobs carry fixed markers, and a crontab written by an older
version migrates by itself.

Older versions stamped every line with a per-home suffix (`# ava-logs-maintenance.ava-<hash>`).
The fixed markers are prefixes of those, and lines are matched by substring, so the
first register after an update replaces each old line in place and the first
unregister removes it: no manual step and no second line per job.

`_OLD_CRONTAB` is the four Ava lines of a production Linux host's real crontab
(read with `crontab -l`), with the user's home directory replaced by
`/home/operator` and the hash of that path by a placeholder. Nothing else was in
that crontab, and no line carried a credential. The WAL-G tick has no older version,
so it has no old line: it is only added and removed.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from base.host.system import cron, logs_job, packages_job, pr_flow_job, walg_job

_OLD_CRONTAB = """\
40 4 * * * AVA_HOME=/home/operator/.ava /bin/sh -c '/home/operator/.ava/source/.venv/bin/ava logs rotate && /home/operator/.ava/source/.venv/bin/ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3'  # ava-logs-maintenance.ava-0a1b2c3d
*/15 * * * * AVA_HOME=/home/operator/.ava /bin/sh -c '/home/operator/.ava/source/.venv/bin/ava packages refresh --from-job' >> /home/operator/.ava/logs/packages-refresh.log 2>&1  # ava-packages-refresh.ava-0a1b2c3d
25 0 * * * AVA_HOME=/home/operator/.ava /bin/sh -c '/home/operator/.ava/source/.venv/bin/python /home/operator/.ava/source/scripts/ci/pull_requests/pr_flow_export.py' >> /home/operator/.ava/logs/pr-flow.out.log 2>&1  # # ava-pr-flow  # ava-pr-flow.ava-0a1b2c3d
*/5 * * * * AVA_HOME=/home/operator/.ava /home/operator/.ava/source/.venv/bin/ava cluster health-probe # ava-health-probe.ava-0a1b2c3d
"""

_MARKERS = (
    "# ava-logs-maintenance",
    "# ava-packages-refresh",
    "# ava-pr-flow",
    "# ava-health-probe",
)
_WALG_MARKER = "# ava-walg"
_REGISTERS = (
    logs_job._register_linux,
    packages_job._register_linux,
    pr_flow_job._register_linux,
    walg_job._register_linux,
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
    fake = _Crontab(_OLD_CRONTAB)
    monkeypatch.setattr(cron.shutil, "which", lambda _name: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cron.subprocess, "run", fake.run)
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/home/operator/.ava/source/.venv/bin/ava")
    return fake


def _register_all() -> None:
    assert cron._register_linux(300) == 0
    for register in _REGISTERS:
        assert register() == 0


@pytest.mark.parametrize("marker", _MARKERS)
def test_one_old_line_carries_each_fixed_marker_as_a_prefix(marker: str) -> None:
    (old_line,) = [line for line in _OLD_CRONTAB.splitlines() if marker in line]
    assert old_line.endswith(f"{marker}.ava-0a1b2c3d")


def test_register_leaves_exactly_one_line_per_job_and_no_old_suffix(crontab: _Crontab) -> None:
    _register_all()

    assert len(crontab.lines) == 5
    assert "ava-0a1b2c3d" not in crontab.text
    for marker in (*_MARKERS, _WALG_MARKER):
        assert len([line for line in crontab.lines if marker in line]) == 1, marker
        # The new line ends with its fixed marker, with no per-home suffix after it.
        assert len([line for line in crontab.lines if line.endswith(marker)]) == 1, marker


def test_registering_again_changes_nothing(crontab: _Crontab) -> None:
    _register_all()
    first = sorted(crontab.lines)
    _register_all()

    assert sorted(crontab.lines) == first


def test_unregister_removes_the_old_lines_and_only_them(crontab: _Crontab) -> None:
    unrelated = "0 3 * * * /usr/local/bin/backup"
    walg = "25 6 * * * /home/operator/.ava/source/.venv/bin/ava backup walg run  # ava-walg"
    crontab.text = unrelated + "\n" + _OLD_CRONTAB + walg + "\n"

    assert cron._unregister_linux() == 0
    for unregister in _UNREGISTERS:
        assert unregister() == 0

    assert crontab.lines == [unrelated]
