"""A legacy stop hold whose failure receipts name only other machines' agents.

FC-10 F20: every runner sees every wake, and the legacy host latched a wake it
received for another machine's agent as a failure receipt of its own hold when
its stop cancelled that wake before the row read. Such a receipt names an agent
outside the cohort the legacy preparation captured, so it is no continuation of
the unit: the adoption records it with its evidence and settles it. One receipt
of a cohort agent is the unit's own and still refuses.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from cli.commands._pause_resume import resume_after_start
from scripts import cutover_adopt_home as adopt
from tests.lifecycle.cutover.conftest import LegacyHome
from tests.lifecycle.cutover.test_adopt_home import Make, _hold, _journal, _run

# The LX unit's hold: its stop drained 2, 6 and 7, then latched fc10-mac2's
# 3, 8 and 9, which that host's resume had woken.
_FOREIGN = {"3": "CancelledError", "8": "CancelledError", "9": "CancelledError"}


def _fc10_lx_hold(legacy: LegacyHome, failures: dict[str, str]) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "state": "paused",
        "holder": "fc10-gw:pid17913",
        "acquired_at": "2026-09-29T05:35:53.041351+00:00",
        "maintenance": {
            "phase": "stopped",
            "commands": {"2": 34, "6": 35, "7": 36},
            "drained": [2, 6, 7],
            "failures": failures,
            "parked": [],
            "reaped": {},
            "repaired": {},
            "repair_record": None,
            "undelivered": {},
        },
        "driver": None,
    }
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(doc))
    return doc


def _adopt(legacy: LegacyHome) -> dict[str, Any]:
    doc = _fc10_lx_hold(legacy, _FOREIGN)
    assert _run(legacy, "--execute", "--cutover-id", "c5") == 0
    return doc


def test_foreign_receipts_are_recorded_with_their_evidence_and_settled(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    _fc10_lx_hold(legacy, _FOREIGN)
    assert _run(legacy) == 0
    assert "hold-disregard-foreign-receipts" in capsys.readouterr().out
    doc = _adopt(legacy)
    journal = _journal(legacy)
    assert journal["hold"]["origin"] == "legacy-stop"
    assert journal["steps"]["hold"]["effects"] == [
        {
            "op": "hold-disregard-foreign-receipts",
            "holder": doc["holder"],
            "acquired_at": doc["acquired_at"],
            "cohort": [2, 6, 7],
            "parked": [],
            "drained": [2, 6, 7],
            "receipts": _FOREIGN,
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


def test_the_held_first_start_passes_the_receipt_gate_after_the_settlement(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsettled, the start refuses exactly as FC-10's did: "start cannot
    release failed continuation/flush receipts; hold retained"."""
    import cli.start_intent
    from shared import maintenance, start_serving
    from shared.config import settings

    legacy = make_legacy()
    _adopt(legacy)

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
    assert (current.maintenance.phase, sorted(current.maintenance.commands)) == (
        "ready",
        [2, 6, 7],
    )


def test_one_receipt_of_a_cohort_agent_refuses_before_any_effect(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    _fc10_lx_hold(legacy, {**_FOREIGN, "7": "CancelledError"})
    before = legacy.snapshot()
    assert _run(legacy, "--execute") == 1
    err = capsys.readouterr().err
    assert "adoption refused, nothing changed" in err
    assert "not a completed stop's maintenance hold" in err
    assert "failure receipts of its own agents [7]" in err
    assert legacy.snapshot() == before


@pytest.mark.parametrize(
    ("failures", "receipts"),
    [
        pytest.param(_FOREIGN, {"3": "CancelledError"}, id="receipts-changed"),
        pytest.param({"7": "CancelledError"}, {"7": "CancelledError"}, id="cohort-agent"),
    ],
)
def test_the_settlement_refuses_anything_but_the_planned_foreign_receipts(
    make_legacy: Make, failures: dict[str, str], receipts: dict[str, str]
) -> None:
    legacy = make_legacy()
    doc = _fc10_lx_hold(legacy, failures)
    effect = {"op": "hold-disregard-foreign-receipts", "receipts": receipts}
    journal = {"cutover_id": "c6", "hold": {k: doc[k] for k in ("holder", "acquired_at")}}
    with pytest.raises(RuntimeError, match="changed under the adoption"):
        adopt._settle_foreign_receipts(legacy.home, journal, effect)
    assert json.loads((legacy.home / "run" / "deploy-pause-owner.json").read_text()) == doc
