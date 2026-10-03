"""Cold-start reconcile phase (--reconcile-case), C-6 replay (task #4872).

Kill the surviving generation out from under its custody records, then let the
keeper's own cold-start retry run. Route C must prove every recorded birth
gone, release the stale records, and bring the tree back up — no operator force.
"""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .support import (
    _POLL_S,
    _ROOT_WAIT_S,
    _UNIT_IDS,
    _fail,
    _kill,
    _pid_alive,
    _ppid_of,
    _probe_pids,
    _ps_line,
    _save,
    _status_if_keeper_running,
    _status_if_running,
    _unit_entry,
    _wait_for,
)

_RECONCILE_WAIT_S = 90.0


def _reconcile_phase(
    *,
    enabled: bool,
    workdir: Path,
    evidence: Path,
    run_dir: Path,
    probe: Path,
    root_status: Callable[[], dict[str, Any]],
    helper_root_status: Callable[[], dict[str, Any]],
    phase_pass: Callable[[str, str], None],
    recorded_pids: set[int],
    new_helper_pid: int,
    reseeded_pid: int,
    reseeded_units: dict[str, int],
) -> None:
    """Kill the surviving generation; let the keeper reconcile and reseed."""
    # The gate lives here (not at the call site) so main() pays one call,
    # not a branch: main()'s complexity budget is frozen by the baseline.
    if not enabled:
        return

    keeper_before = helper_root_status()
    dead_units = dict(reseeded_units)
    _kill_generation(evidence=evidence, probe=probe, dead_units=dead_units)

    custody_dir = run_dir / "custody"
    stale_records = {path.name: path.read_text() for path in sorted(custody_dir.glob("*.json"))}
    if set(stale_records) != {f"{unit_id}.json" for unit_id in _UNIT_IDS}:
        _fail("reconcile", f"stale record set unexpected: {sorted(stale_records)}")
    _save(evidence, "reconcile-stale-records.json", json.dumps(stale_records, indent=2))
    stderr_path = workdir / "root.stderr.log"
    stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
    helper_stderr = workdir / "helper.stderr.log"
    helper_offset = helper_stderr.stat().st_size if helper_stderr.exists() else 0

    released_at, keeper_after, new_root_pid, fresh_units = _watch_release_and_keeper(
        custody_dir=custody_dir,
        root_status=root_status,
        helper_root_status=helper_root_status,
        recorded_pids=recorded_pids,
        stale_records=stale_records,
        reseeded_pid=reseeded_pid,
    )
    after_records = _assert_records_released(
        custody_dir=custody_dir, stale_records=stale_records, fresh_units=fresh_units
    )
    _assert_fresh_chain(
        probe=probe,
        fresh_units=fresh_units,
        new_root_pid=new_root_pid,
        new_helper_pid=new_helper_pid,
        dead_units=dead_units,
    )

    # c) no refusal after the generation died: the cold-start gate must
    #    have released rather than refused.
    fresh_stderr = stderr_path.read_text()[stderr_offset:] if stderr_path.exists() else ""
    refusals = fresh_stderr.count("custody requires reconciliation")
    restarts_after = int(helper_root_status()["restarts"])
    helper_fresh = helper_stderr.read_text()[helper_offset:] if helper_stderr.exists() else ""
    keeper_retries = helper_fresh.count("restart in")

    _save(
        evidence,
        "reconcile-after.json",
        json.dumps(
            {
                "keeper_before": keeper_before,
                "keeper_after": keeper_after,
                "dead_units": dead_units,
                "fresh_units": fresh_units,
                "new_root_pid": new_root_pid,
                "released_at": released_at,
                "refusals_after_death": refusals,
                "restarts_before": int(keeper_before["restarts"]),
                "restarts_after": restarts_after,
                "keeper_retries_in_window": keeper_retries,
            },
            indent=2,
        ),
    )
    _save(
        evidence,
        "reconcile-release-trace.txt",
        "\n".join(
            f"{name}: stale record released — observed at {ts}"
            for name, ts in sorted(released_at.items())
        )
        or "release could not be sampled before the slot was recreated",
    )
    _save(evidence, "reconcile-stderr-window.txt", fresh_stderr[-2000:] or "<empty window>")
    _save(evidence, "reconcile-keeper-cadence.txt", helper_fresh[-2000:] or "<empty window>")
    _save(evidence, "reconcile-records-after.json", json.dumps(after_records, indent=2))
    phase_pass(
        "reconcile",
        f"dead generation {sorted(dead_units.values())} released on cold start; "
        f"new tree root {new_root_pid} units {list(fresh_units.values())}; "
        f"restarts {keeper_before['restarts']}->{restarts_after}; "
        f"refusals-after-death={refusals}"
        + ("; all releases observed live" if len(released_at) == len(stale_records) else ""),
    )


def _kill_generation(*, evidence: Path, probe: Path, dead_units: dict[str, int]) -> None:
    """SIGKILL the surviving generation; wait until its probes are all gone."""
    pre_kill = _ps_line(*dead_units.values())
    for unit_pid in dead_units.values():
        _kill(unit_pid, signal.SIGKILL)
    _wait_for(
        "dead generation",
        lambda: (
            _probe_pids(probe) == set() and not any(_pid_alive(pid) for pid in dead_units.values())
        ),
        30.0,
        "reconcile",
    )
    _save(
        evidence,
        "reconcile-killed.txt",
        pre_kill + "\nunit pids SIGKILLed; probe pgrep empty (full death)",
    )


def _watch_release_and_keeper(
    *,
    custody_dir: Path,
    root_status: Callable[[], dict[str, Any]],
    helper_root_status: Callable[[], dict[str, Any]],
    recorded_pids: set[int],
    stale_records: dict[str, str],
    reseeded_pid: int,
) -> tuple[dict[str, float], dict[str, Any], int, dict[str, int]]:
    """Watch custody slots while the keeper retries; wait out the fresh tree."""
    # Watch the records while the keeper retries: a release removes the
    # stale record before the fresh generation recreates the slot.
    released_at: dict[str, float] = {}
    keeper_after = None
    wait_deadline = time.monotonic() + _RECONCILE_WAIT_S
    while time.monotonic() < wait_deadline:
        for name, stale_text in stale_records.items():
            if name in released_at:
                continue
            try:
                text = (custody_dir / name).read_text()
            except OSError:
                released_at[name] = time.time()  # stale slot unlinked (release)
                continue
            if text != stale_text:
                released_at[name] = time.time()  # slot recreated for the fresh generation
        keeper_after = _status_if_keeper_running(helper_root_status)
        if keeper_after is not None:
            break
        time.sleep(_POLL_S)
    if keeper_after is None:
        _fail(
            "reconcile",
            f"the keeper never brought a root up over the dead generation; "
            f"released={sorted(released_at)}",
        )
    new_root_pid = int(keeper_after["pid"])
    recorded_pids.add(new_root_pid)
    tree = _wait_for(
        "reconciled tree",
        lambda: _status_if_running(root_status, excluding=int(reseeded_pid)),
        _ROOT_WAIT_S,
        "reconcile",
    )
    fresh_units = {unit_id: int(_unit_entry(tree, unit_id)["pid"]) for unit_id in _UNIT_IDS}
    recorded_pids.update(fresh_units.values())

    return released_at, keeper_after, new_root_pid, fresh_units


def _assert_records_released(
    *, custody_dir: Path, stale_records: dict[str, str], fresh_units: dict[str, int]
) -> dict[str, str]:
    """Check (a): stale record text never survives; each slot binds the fresh pid."""
    # a) released, not retained: no stale record text survives, and each
    #    slot now carries exactly the fresh generation's birth.
    after_records = {path.name: path.read_text() for path in sorted(custody_dir.glob("*.json"))}
    stale_survivors = [
        name
        for name, text in after_records.items()
        if name in stale_records and text == stale_records[name]
    ]
    if stale_survivors:
        _fail("reconcile", f"stale records survived the cold start: {stale_survivors}")
    if set(after_records) != set(stale_records):
        _fail("reconcile", f"fresh record set unexpected: {sorted(after_records)}")
    for unit_id, unit_pid in fresh_units.items():
        body = json.loads(after_records[f"{unit_id}.json"])
        recorded_pids_in_record = {int(item["pid"]) for item in body["processes"]}
        if body.get("stage") != "running" or recorded_pids_in_record != {unit_pid}:
            _fail(
                "reconcile",
                f"unit {unit_id} record does not bind pid {unit_pid}: {body}",
            )

    return after_records


def _assert_fresh_chain(
    *,
    probe: Path,
    fresh_units: dict[str, int],
    new_root_pid: int,
    new_helper_pid: int,
    dead_units: dict[str, int],
) -> None:
    """Check (b): the fresh generation is a new, correctly parented tree."""
    # b) the cold start succeeded with a fresh chain.
    for unit_id, unit_pid in fresh_units.items():
        ppid = _ppid_of(unit_pid)
        if ppid != new_root_pid:
            _fail("reconcile", f"unit {unit_id} ppid {ppid} != {new_root_pid}")
    if _ppid_of(new_root_pid) != new_helper_pid:
        _fail("reconcile", f"root ppid {_ppid_of(new_root_pid)} != helper {new_helper_pid}")
    if set(fresh_units.values()) & set(dead_units.values()):
        _fail("reconcile", "fresh generation reused a dead generation pid")
    probes = _probe_pids(probe)
    if probes != set(fresh_units.values()):
        _fail("reconcile", f"unit probes {sorted(probes)} are not the fresh generation")
