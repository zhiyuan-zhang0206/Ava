"""The fleet cutover's maintenance hold, which only the cutover flow releases.

`scripts/cutover_adopt_home.py` records the hold it adopts (a completed legacy
stop's) or creates in its journal, `ADOPTION_JOURNAL`. While exactly that hold
stands, no ordinary path releases it: not a typed `ava start`, not the
autostart job after a reboot, not `ava cluster recover` and not
`ava maintenance resume`. Business opens only at the go/no-go gate, through
the script's `--resume` step (`release_command`), which checks the cutover's
own records first.

- Before the held first start (phase `stopped`) an ordinary start refuses and
  names `cutover_adopt_home.py --start`, and so does `ava maintenance start`
  with the hold's exact generation. On a gateway that start follows the
  data-plane cutover and the database-records repair.
- After it (phase `starting` or `ready`) an ordinary start brings the unit up
  and keeps the hold. One that passes readiness completes a `starting` hold (a
  failed or unready held first start) to `ready`, as the held first start does.

Any other hold, including a later stop's on an adopted home, keeps the
ordinary release. The adoption also reads a legacy stop's hold here
(`legacy_hold_facts`) and settles its failure receipts that carry no lost work
(`settle_receipts`). Deleted with the `scripts/cutover_*` scripts after the
cutover.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.maintenance_state import MaintenanceHold

ADOPTION_JOURNAL = "cutover-rollback/adopt-home.json"
# The read ceiling of the journals under `cutover-rollback/`: this adoption journal
# and the database-records one (`scripts/cutover_db_records.py`), the large one. It keeps
# the before image of each identity-less row it mints, about 670 bytes as written:
# FC-10 pass B wrote 3,624,220 bytes for 5,407 rows, and production holds 5,433,
# about 3.7 MB. 32 MiB is about 9x that (some 50,000 rows) and still bounds what a
# damaged or foreign file makes a reader load.
CUTOVER_JOURNAL_MAX_BYTES = 32 * 1024 * 1024
# The holder of a hold the cutover creates (`cutover:<id>`); no other hold takes it.
CUTOVER_HOLDER_PREFIX = "cutover:"


@dataclass(frozen=True)
class CutoverHold:
    """The `(holder, acquired_at)` generation the adoption journal recorded."""

    holder: str
    acquired_at: datetime


# The legacy rules that make every unsettled receipt on a `stopped` hold a
# post-drain one: they hold from this commit (#1872, the maintenance journal's
# introduction) on, unchanged at every later commit writing a maintenance phase.
LEGACY_RECEIPT_RULES_SINCE = "cc5c5e2098385fa0e28e42b882e273f0bdecdfed"
LEGACY_RECEIPT_RULES = (
    "shared/maintenance.py::set_phase: entering `drained` requires no unsettled "
    "failure, and no other code moves a hold past `draining`",
    "shared/maintenance.py::record_drained: a drained receipt is refused once the "
    "agent has a failure",
)
# Classes the adoption settles: other machines' agents, then members that have
# nothing to continue after the certified drain.
SETTLED_CLASSES = ("foreign", "drained", "parked")


def legacy_hold_facts(home: Path, checkout: Path) -> dict[str, Any]:
    """`home`'s pause-owner journal as the inventory reports it, and whether the
    adoption may keep its hold as the cutover hold.

    A completed legacy `ava stop` leaves its hold in phase `stopped`. Its
    unsettled receipts are classified by agent: `foreign` (outside the cohort
    the legacy preparation captured, so no continuation in the hold: the host
    latched other machines' woken agents, FC-10 F20), `drained`, `parked`, and
    `other` (a member neither drained nor reaped, which a certified drain
    rules out). Foreign ones always settle. Drained and parked ones settle
    when the legacy code carries `LEGACY_RECEIPT_RULES` (`legacy_rules`): those
    receipts then postdate the certified drain, and such a wake claims nothing.
    Any other receipt refuses the adoption.
    """
    from shared import pause_owner

    snapshot = pause_owner.read_for_home(home)
    facts: dict[str, Any] = {
        "status": snapshot.status,
        "holder": snapshot.holder,
        "acquired_at": snapshot.acquired_at.isoformat() if snapshot.acquired_at else None,
        **_maintenance_facts(snapshot.maintenance, home, checkout),
    }
    facts["adoptable"] = (
        snapshot.status == "paused"
        and facts["maintenance_phase"] == "stopped"
        and not facts["unsettleable"]
    )
    return facts


def _maintenance_facts(hold: MaintenanceHold | None, home: Path, checkout: Path) -> dict[str, Any]:
    if hold is None:
        return {
            "maintenance_phase": None,
            "cohort": [],
            "parked": [],
            "drained": [],
            "receipts": {},
            "legacy_rules": None,
            "settle": {},
            "unsettleable": [],
        }
    unsettled = hold.unsettled_failures()
    classes = _classify(hold, unsettled)
    rules = None
    if hold.phase == "stopped" and (classes["drained"] or classes["parked"]):
        rules = legacy_rules(home, checkout)
    settle = _settleable(classes, post_drain=bool(rules and rules["hold"]))
    return {
        "maintenance_phase": hold.phase,
        "cohort": sorted(hold.commands),
        "parked": list(hold.parked),
        "drained": list(hold.drained),
        "receipts": {name: _keyed(receipts) for name, receipts in classes.items()},
        "legacy_rules": rules,
        "settle": _keyed(settle),
        "unsettleable": sorted(set(unsettled) - set(settle)),
    }


def _keyed(receipts: dict[int, str]) -> dict[str, str]:
    """Receipts keyed as the journal writes them, ordered by agent."""
    return {str(agent): cause for agent, cause in sorted(receipts.items())}


def _settleable(classes: dict[str, dict[int, str]], *, post_drain: bool) -> dict[int, str]:
    """Foreign receipts, and with the legacy rules proven also drained and parked ones."""
    settle: dict[int, str] = {}
    for name in SETTLED_CLASSES if post_drain else ("foreign",):
        settle |= classes[name]
    return settle


def _classify(hold: MaintenanceHold, unsettled: dict[int, str]) -> dict[str, dict[int, str]]:
    foreign = hold.receipts_outside_cohort()
    drained = {agent: cause for agent, cause in unsettled.items() if agent in hold.drained}
    parked = {agent: cause for agent, cause in unsettled.items() if agent in hold.parked}
    known = set(foreign) | set(drained) | set(parked)
    other = {agent: cause for agent, cause in unsettled.items() if agent not in known}
    return {"foreign": foreign, "drained": drained, "parked": parked, "other": other}


def legacy_rules(home: Path, checkout: Path) -> dict[str, Any]:
    """Whether the legacy code that stopped `home` carries `LEGACY_RECEIPT_RULES`.

    The legacy commit is `installed_sha`, the commit the legacy code last
    installed and started (by adoption time the checkout already moved to the
    new code); `git merge-base --is-ancestor` in the owning checkout decides.
    Anything unreadable leaves the rules unproven (`hold` false, with `reason`).
    """
    import re
    import subprocess

    from shared.deploy.git.gitenv import git_env
    from shared.proc import run_bounded

    since = LEGACY_RECEIPT_RULES_SINCE
    evidence: dict[str, Any] = {
        "legacy_commit": None,
        "rules_since": since,
        "rules": list(LEGACY_RECEIPT_RULES),
        "hold": False,
    }
    try:
        legacy = (home / "installed_sha").read_text().strip()
    except OSError as exc:
        return {**evidence, "reason": f"no legacy commit: installed_sha is unreadable ({exc})"}
    if not re.fullmatch(r"[0-9a-f]{40}", legacy):
        return {**evidence, "reason": f"installed_sha is not a commit id: {legacy[:60]!r}"}
    evidence["legacy_commit"] = legacy
    argv = ["git", "-C", str(checkout), "merge-base", "--is-ancestor", since, legacy]
    try:
        result = run_bounded(argv, capture_output=True, text=True, env=git_env(), timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {**evidence, "reason": f"git merge-base could not run: {exc}"}
    if result.returncode == 0:
        return {**evidence, "hold": True, "reason": f"{legacy} descends from {since}"}
    if result.returncode == 1:
        return {**evidence, "reason": f"{legacy} does not descend from {since}"}
    detail = result.stderr.strip()[:200]
    return {**evidence, "reason": f"git merge-base in {checkout} failed: {detail}"}


def settle_receipts(
    hold: MaintenanceHold, receipts: dict[int, str], record: dict[str, str], *, post_drain: bool
) -> MaintenanceHold | None:
    """`hold` with exactly `receipts` moved into `repaired` under `record`;
    None when an earlier run already did.

    Refuses unless the hold is `stopped`, its unsettled failures are exactly
    `receipts`, each foreign or (with `post_drain`, the legacy rules proven)
    of a drained or parked member, and no repair record stands: an operator's
    is never replaced.
    """
    from dataclasses import replace

    from shared.maintenance_state import validate_repair_record

    unsettled = hold.unsettled_failures()
    if not unsettled and receipts.items() <= hold.repaired.items():
        return None
    allowed = _settleable(_classify(hold, unsettled), post_drain=post_drain)
    if hold.phase != "stopped" or unsettled != receipts or not receipts.keys() <= allowed.keys():
        raise RuntimeError(
            f"the hold's failure receipts changed under the adoption ({hold.phase}, "
            f"{unsettled}, planned {receipts}); run the inventory again"
        )
    if hold.repair_record is not None:
        raise RuntimeError("the hold already carries an operator's repair record")
    failures = {agent: cause for agent, cause in hold.failures.items() if agent not in receipts}
    return replace(
        hold,
        failures=failures,
        repaired={**hold.repaired, **receipts},
        repair_record=validate_repair_record(record),
    )


def recorded_hold(home: Path) -> CutoverHold | None:
    """The hold `home`'s adoption journal recorded; None when it was never adopted."""
    from shared.verified_file import regular_bytes

    path = home / ADOPTION_JOURNAL
    try:
        journal = json.loads(regular_bytes(path, max_bytes=CUTOVER_JOURNAL_MAX_BYTES))
        named = Path(journal["home"])
        hold = CutoverHold(
            journal["hold"]["holder"], datetime.fromisoformat(journal["hold"]["acquired_at"])
        )
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"unreadable adoption journal {path}: {exc}") from exc
    if named.resolve() != home.resolve():
        raise RuntimeError(f"adoption journal {path} names another home: {named}")
    return hold


def standing_hold(home: Path) -> CutoverHold | None:
    """The recorded cutover hold, only while it is the hold standing on `home`."""
    from shared import pause_owner

    recorded = recorded_hold(home)
    if recorded is None:
        return None
    current = pause_owner.read_for_home(home)
    if current.status == "paused" and current.matches(recorded.holder, recorded.acquired_at):
        return recorded
    return None


def held_start_command(home: Path) -> str:
    return f"`.venv/bin/python scripts/cutover_adopt_home.py --home {home} --start`"


def release_command(home: Path) -> str:
    """The go/no-go step: the one exit of the cutover hold."""
    return f"`.venv/bin/python scripts/cutover_adopt_home.py --home {home} --resume`"


def resume_refusal(home: Path, holder: str, acquired_at: datetime) -> str | None:
    """Why `ava maintenance resume` must not release `(holder, acquired_at)`; None
    when it is not the recorded cutover hold.

    An unreadable journal cannot tell an adopted legacy stop's hold (named
    `local-pause:`) from a later stop's, so unlike an ordinary start, the
    exact-holder resume then proceeds and says so: with `ava maintenance start`,
    it is the way out of such a journal for a later hold
    (conventions/cutover-home-adoption.md). The journal is the cutover's alone,
    so a hold another subsystem took (`fleet:`, `pitr:`, `recovery:`) resumes
    the same way. Only a created cutover hold stays refused: it is
    `cutover:<id>`, a name no later hold takes, so damage to the journal opens
    no exit around the go/no-go step.
    """
    return _exact_holder_refusal(
        home,
        holder,
        acquired_at,
        unreadable=(
            f"a `{CUTOVER_HOLDER_PREFIX}` hold never resumes without it. Repair the "
            f"journal; the go/no-go gate releases the cutover hold with {release_command(home)}"
        ),
        proceeding=f"resuming {holder} as named",
        recorded=(
            f"{holder} is the fleet cutover's hold, which `ava maintenance resume` never "
            f"releases; the go/no-go gate releases it with {release_command(home)}"
        ),
    )


def start_refusal(home: Path, holder: str, acquired_at: datetime) -> str | None:
    """Why `ava maintenance start` must not start `(holder, acquired_at)` from
    phase `stopped`; None when it is not the recorded cutover hold.

    That start is the cutover's held first start, which on a gateway waits for
    the database-records repair (W7 before W8) and on a remote unit installs
    its capability bundle, so this verb refuses it as an ordinary start does.
    From phase `starting` on, it starts held as an ordinary start does. An
    unreadable journal is read as `resume_refusal` reads it: a later hold
    starts as named, a `cutover:<id>` hold stays refused.
    """
    return _exact_holder_refusal(
        home,
        holder,
        acquired_at,
        unreadable=(
            f"a `{CUTOVER_HOLDER_PREFIX}` hold never starts without it. Repair the journal; "
            f"its first start is {held_start_command(home)}"
        ),
        proceeding=f"starting {holder} as named",
        recorded=(
            f"{holder} is the fleet cutover's hold in phase stopped; its first start is "
            f"{held_start_command(home)} (a remote unit adds `--db-capability BUNDLE`), "
            "never `ava maintenance start`"
        ),
    )


def _exact_holder_refusal(
    home: Path,
    holder: str,
    acquired_at: datetime,
    *,
    unreadable: str,
    proceeding: str,
    recorded: str,
) -> str | None:
    """`recorded` when `(holder, acquired_at)` is the journal's cutover hold.

    An unreadable journal refuses a `cutover:<id>` holder with `unreadable`
    and lets any other holder through, printing `proceeding`.
    """
    try:
        journal = recorded_hold(home)
    except RuntimeError as exc:
        if holder.startswith(CUTOVER_HOLDER_PREFIX):
            return f"{exc}; {unreadable}"
        print(f"  ! {exc}; {proceeding}", file=sys.stderr)
        return None
    if journal is None or (journal.holder, journal.acquired_at) != (holder, acquired_at):
        return None
    return recorded


def start_instruction(home: Path) -> str:
    """How an operator starts `home` after its data-plane conversion."""
    if standing_hold(home) is None:
        return "`ava start`"
    return (
        "the database-records repair (`scripts/cutover_db_records.py`), then the held "
        f"first start {held_start_command(home)}; never bare `ava start` while the "
        "cutover hold stands"
    )
