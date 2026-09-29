"""A `stopped` legacy stop hold whose failure receipts carry no lost work.

FC-10 F20: every runner receives every wake, and the legacy host latched a wake
it cancelled at its stop as a failure receipt of its own hold. On a `stopped`
hold every unsettled receipt postdates the certified drain when the legacy code
carries the rules in `cli.cutover_hold.LEGACY_RECEIPT_RULES` (from
`LEGACY_RECEIPT_RULES_SINCE` on). The adoption settles, by class, receipts of
other machines' agents (always) and of drained or parked members (only with
those rules proven for the home's `installed_sha`), recording the evidence.
Anything else refuses, and so does a hold that is not `stopped`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cli import cutover_hold
from cli.commands.lifecycle._pause_resume import resume_after_start
from scripts import cutover_adopt_home as adopt
from tests.lifecycle.cutover.conftest import LegacyHome
from tests.lifecycle.cutover.test_adopt_home import Make, _hold, _journal, _run

# The LX unit's hold: its stop drained 2, 6 and 7 (5 parked), then latched
# fc10-mac2's 3, 8 and 9, which that host's resume had woken.
_FOREIGN = {"3": "CancelledError", "8": "CancelledError", "9": "CancelledError"}
_DRAINED = {"2": "CancelledError"}
_PARKED = {"5": "CancelledError"}
_ALL = {**_FOREIGN, **_DRAINED, **_PARKED}


def _lx_hold(
    legacy: LegacyHome,
    failures: dict[str, str],
    *,
    phase: str = "stopped",
    drained: tuple[int, ...] = (2, 6, 7),
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "state": "paused",
        "holder": "fc10-gw:pid17913",
        "acquired_at": "2026-09-29T05:35:53.041351+00:00",
        "maintenance": {
            "phase": phase,
            "commands": {"2": 34, "6": 35, "7": 36},
            "drained": list(drained),
            "failures": failures,
            "parked": [5],
            "reaped": {},
            "repaired": {},
            "repair_record": None,
            "undelivered": {},
        },
        "driver": None,
    }
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(doc))
    return doc


def _git(repo: Path, *args: str) -> str:
    argv = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    done = subprocess.run([*argv, "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603
    return done.stdout.decode().strip()


def _legacy_commit(legacy: LegacyHome, monkeypatch: pytest.MonkeyPatch, *, proven: bool) -> str:
    """Make the owning checkout a repository whose rules commit precedes, or is
    unrelated to, the commit the home's `installed_sha` names."""
    repo = legacy.checkout
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "rules")
    since = _git(repo, "rev-parse", "HEAD")
    if proven:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "legacy")
    else:
        _git(repo, "checkout", "-q", "--orphan", "unrelated")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "legacy")
    legacy_sha = _git(repo, "rev-parse", "HEAD")
    (legacy.home / "installed_sha").write_text(f"{legacy_sha}\n")
    monkeypatch.setattr(cutover_hold, "LEGACY_RECEIPT_RULES_SINCE", since)
    return legacy_sha


def _classes(foreign: dict[str, str], drained: dict[str, str], parked: dict[str, str]) -> Any:
    return {"foreign": foreign, "drained": drained, "parked": parked}


def test_foreign_receipts_settle_without_the_legacy_rules(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixture's `installed_sha` is no commit id: only foreign ones settle."""
    legacy = make_legacy()
    doc = _lx_hold(legacy, _FOREIGN)
    assert _run(legacy) == 0
    assert "hold-disregard-receipts" in capsys.readouterr().out
    assert _run(legacy, "--execute", "--cutover-id", "c5") == 0
    journal = _journal(legacy)
    assert journal["hold"]["origin"] == "legacy-stop"
    assert journal["steps"]["hold"]["effects"] == [
        {
            "op": "hold-disregard-receipts",
            "holder": doc["holder"],
            "acquired_at": doc["acquired_at"],
            "cohort": [2, 6, 7],
            "parked": [5],
            "drained": [2, 6, 7],
            "receipts": _classes(_FOREIGN, {}, {}),
            "evidence": {
                "foreign": "outside the cohort the legacy preparation captured",
                "post_drain": None,
            },
        }
    ]
    hold = _hold(legacy)
    assert hold.matches(doc["holder"], datetime.fromisoformat(doc["acquired_at"]))
    settled = hold.maintenance
    assert settled is not None
    assert (settled.phase, settled.failures) == ("stopped", {})
    assert settled.repaired == {int(agent): cause for agent, cause in _FOREIGN.items()}
    assert settled.repair_record is not None
    assert settled.repair_record["by"] == "cutover adoption c5: foreign receipts disregarded"
    after = legacy.snapshot()
    assert _run(legacy, "--execute") == 0
    assert legacy.snapshot() == after


def test_every_class_settles_with_the_legacy_rules_proven(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    legacy_sha = _legacy_commit(legacy, monkeypatch, proven=True)
    _lx_hold(legacy, _ALL)
    assert _run(legacy, "--execute", "--cutover-id", "c7") == 0
    (effect,) = _journal(legacy)["steps"]["hold"]["effects"]
    assert effect["receipts"] == _classes(_FOREIGN, _DRAINED, _PARKED)
    proof = effect["evidence"]["post_drain"]
    assert (proof["legacy_commit"], proof["hold"]) == (legacy_sha, True)
    assert proof["rules_since"] == cutover_hold.LEGACY_RECEIPT_RULES_SINCE
    assert proof["rules"] == list(cutover_hold.LEGACY_RECEIPT_RULES)
    settled = _hold(legacy).maintenance
    assert settled is not None
    assert settled.failures == {}
    assert settled.repaired == {int(agent): cause for agent, cause in _ALL.items()}


def test_the_held_first_start_passes_the_receipt_gate_after_the_settlement(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsettled, the start refuses exactly as FC-10's did: "start cannot
    release failed continuation/flush receipts; hold retained"."""
    import cli.start_intent
    from shared.config import settings
    from shared.deploy.lifecycle import start_serving
    from shared.deploy.maintenance import admission as maintenance

    legacy = make_legacy()
    _legacy_commit(legacy, monkeypatch, proven=True)
    _lx_hold(legacy, _ALL)
    assert _run(legacy, "--execute", "--cutover-id", "c5") == 0

    @resume_after_start
    def start(_args: object) -> int:
        """The ordinary start's release boundary, its body stubbed."""
        return 0

    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    monkeypatch.setattr(cli.start_intent, "run_start", start)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    assert adopt.main(["--home", str(legacy.home), "--start"], checkout=legacy.checkout) == 0
    current = maintenance.snapshot()
    assert current is not None
    assert current.maintenance is not None
    assert current.maintenance.phase == "ready"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("unrelated", "does not descend from"),
        ("no-installed-sha", "installed_sha is unreadable"),
        ("no-repository", "git merge-base in"),
    ],
)
def test_without_the_legacy_rules_only_foreign_receipts_settle(
    make_legacy: Make,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    reason: str,
) -> None:
    legacy = make_legacy()
    if setup == "unrelated":
        _legacy_commit(legacy, monkeypatch, proven=False)
    elif setup == "no-installed-sha":
        (legacy.home / "installed_sha").unlink()
    else:
        (legacy.home / "installed_sha").write_text(f"{'a' * 40}\n")
    _lx_hold(legacy, {**_FOREIGN, **_DRAINED})
    facts = cutover_hold.legacy_hold_facts(legacy.home, legacy.checkout)
    assert (facts["settle"], facts["unsettleable"]) == (_FOREIGN, [2])
    before = legacy.snapshot()
    assert _run(legacy, "--execute") == 1
    err = capsys.readouterr().err
    assert "failure receipts the adoption cannot settle [2]; legacy rules unproven" in err
    assert reason in err
    assert legacy.snapshot() == before


@pytest.mark.parametrize(
    ("phase", "drained", "failures", "refusal"),
    [
        # FC-10's LX hold after the legacy updater's refused start.
        pytest.param("starting", (2, 6, 7), _ALL, "phase starting", id="lx-starting"),
        # Not `stopped` refuses even when every receipt is foreign.
        pytest.param("starting", (2, 6, 7), _FOREIGN, "phase starting", id="foreign-starting"),
        # A member neither drained nor reaped contradicts a certified drain.
        pytest.param("stopped", (6, 7), _ALL, "cannot settle [2]", id="undrained-member"),
    ],
)
def test_holds_the_proof_does_not_cover_refuse(
    make_legacy: Make,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    drained: tuple[int, ...],
    failures: dict[str, str],
    refusal: str,
) -> None:
    legacy = make_legacy()
    _legacy_commit(legacy, monkeypatch, proven=True)
    _lx_hold(legacy, failures, phase=phase, drained=drained)
    before = legacy.snapshot()
    assert _run(legacy, "--execute") == 1
    err = capsys.readouterr().err
    assert "not a completed stop's maintenance hold" in err and refusal in err
    assert legacy.snapshot() == before


@pytest.mark.parametrize(
    ("phase", "failures", "classes", "post_drain"),
    [
        pytest.param(
            "stopped", _FOREIGN, {"foreign": {"3": "CancelledError"}}, False, id="changed"
        ),
        pytest.param("stopped", _DRAINED, {"drained": _DRAINED}, False, id="unproven-drained"),
        pytest.param("stopped", _PARKED, {"parked": _PARKED}, False, id="unproven-parked"),
        pytest.param("starting", _DRAINED, {"drained": _DRAINED}, True, id="not-stopped"),
    ],
)
def test_the_settlement_refuses_anything_but_the_planned_receipts(
    make_legacy: Make,
    phase: str,
    failures: dict[str, str],
    classes: dict[str, dict[str, str]],
    post_drain: bool,
) -> None:
    legacy = make_legacy()
    doc = _lx_hold(legacy, failures, phase=phase)
    rules = {"hold": True} if post_drain else None
    effect = {"receipts": classes, "evidence": {"foreign": "x", "post_drain": rules}}
    journal = {"cutover_id": "c6", "hold": {k: doc[k] for k in ("holder", "acquired_at")}}
    with pytest.raises(RuntimeError, match="changed under the adoption"):
        adopt._settle_receipts(legacy.home, journal, effect)
    assert json.loads((legacy.home / "run" / "deploy-pause-owner.json").read_text()) == doc
