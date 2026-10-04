"""A killed member of a unit's group is a zombie until its parent reaps it, and a zombie fills its group.

`services/supervision/ava_root/tests/test_ava_root_supervisor.py` kills a stray member and then asks root for a final
stop that must find the group empty. `gone()` counts a zombie as ended (the test is not its
reaper), but `group_empty` counts a zombie as a member by design (`base/native_process/
group_closure.py`), so the stop refuses until the parent reaps. An orphan's parent is init, which
reaps within milliseconds; root's stop window in those tests is 200 ms, so the outcome was a
race: `conventions/flaky-tests.md` section 1, and CI's attempt 1 red / attempt 2 green.

Here the parent is a process this test controls, so the zombie lasts exactly as long as the test
wants and the timing the race left to chance is fixed.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from base.native_process.group_closure import group_empty, group_members
from services.supervision.ava_root.tests.test_ava_root_supervisor import (
    ended,
    exited,
    exits_on,
    gone,
    kill_all,
    pid_in,
    root,
)


async def test_a_killed_member_fills_the_group_until_its_parent_reaps_it(tmp_path: Path) -> None:
    member_file, reap, trigger = (tmp_path / name for name in ("member", "reap", "go"))
    # The holder leads a group of its own (outside the stop) and does not wait on its child
    # until told to. The child joins the unit's group, so once killed it is a zombie there.
    holder = (
        "import pathlib,subprocess,sys,time\n"
        "member=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        " process_group=int(sys.argv[1]))\n"
        f"pathlib.Path({str(member_file)!r}).write_text(str(member.pid))\n"
        f"while not pathlib.Path({str(reap)!r}).exists(): time.sleep(0.01)\n"
        "member.wait()\n"
    )
    spawn = (
        "import os,subprocess\n"
        f"subprocess.Popen([sys.executable,'-c',{holder!r},str(os.getpgid(0))],process_group=0)\n"
        f"while not pathlib.Path({str(member_file)!r}).exists(): time.sleep(0.01)"
    )
    owner = root(tmp_path, exits_on(trigger, spawn))
    await owner.start()
    member = psutil.Process(await pid_in(member_file))
    try:
        pgid = os.getpgid(member.pid)
        trigger.touch()
        await exited(owner)
        # Root's stop kills the member. Its parent has not reaped it, so it stays in the unit's
        # group as a zombie and the stop cannot prove the group empty.
        with pytest.raises(RuntimeError, match=f"process group {pgid} still holds pids"):
            await owner.down("worker")
        assert ended(member), "`gone()` is satisfied: the member is dead"
        assert member.is_running(), "yet it is still in the process table"
        assert group_members(pgid) == [member.pid]
        assert not group_empty(pgid)
        # Once its parent reaps it, the wait the tests now use returns and the stop settles.
        reap.touch()
        await gone(member, reaped=True)
        assert group_empty(pgid)
        await owner.down("worker")
        assert not list((tmp_path / "custody").iterdir())
    finally:
        reap.touch()
        kill_all([member])
    await owner.shutdown()
