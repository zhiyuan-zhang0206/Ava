"""The golden image build is safe to read before it runs, and judges the grant honestly.

A real build pulls a 27 GB image and needs a person to click two toggles, so it is never
run in a test. `--dry-run` prints every step and touches nothing; the verdict on the
grant is a pure function of what the guest reported.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.verify import tart_golden
from scripts.verify.boundary import RunFailedError
from scripts.verify.tart_vm import Tart

SHA1 = "CF503E0148DD7F8349FC23EE3D414CF40B925E37"
GRANTED: dict[str, Any] = {
    "ping": {"preflight_screen": True, "ax_trusted": True},
    "identity": f'  1) {SHA1} "Ava Permissions Helper Code Signing" (CSSMERR_TP_NOT_TRUSTED)',
    "signature": [
        'designated => identifier "com.ava.permissions-helper" and certificate leaf = '
        f'H"{SHA1.lower()}"',
        "CDHash=c2cd6485ef1fa91d10ff82fec89864ac666fe394",
    ],
    "tcc": {
        "kTCCServiceScreenCapture": [2, f"FADE0C00{SHA1}"],
        "kTCCServiceAccessibility": [2, f"FADE0C00{SHA1}"],
    },
    "capture": {"distinct": 270171, "top_share": 0.36},
}


def test_a_dry_run_prints_every_step_in_order_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Path(__file__).resolve().parents[3]
    evidence_root = tmp_path / "evidence"
    # A binary that does not exist: a dry run that tried to run Tart would fail loudly.
    tart = Tart(str(tmp_path / "no-tart-here"), dry_run=True)

    status = tart_golden.build_golden(
        "HEAD", evidence_root, name="ava-golden", repo=repo, tart=tart
    )

    printed = capsys.readouterr().out
    steps = re.findall(r"^\[dry-run\] step (\S+):", printed, re.M)
    assert status == 0
    assert steps == [
        "clone", "configure", "prepare-source", "toolchain", "python", "identity",
        "key-access", "helper", "restart-helper", "verify", "finalize",
    ]  # fmt: skip
    assert not evidence_root.exists()
    # The click is the one human step, with instructions and a wait before the check.
    assert printed.index("Screen & System Audio Recording") < printed.index("restart-helper")
    assert "press Enter" in printed


def test_the_golden_build_never_overwrites_a_vm_or_boots_a_third(tmp_path: Path) -> None:
    class Listing(Tart):
        def __init__(self, vms: list[dict[str, Any]]) -> None:
            super().__init__(str(tmp_path / "unused"))
            self.rows = vms

        def vms(self) -> list[dict[str, Any]]:
            return self.rows

    repo = Path(__file__).resolve().parents[3]
    exists = Listing([{"Name": "ava-golden", "Running": False, "Source": "local"}])
    with pytest.raises(RunFailedError, match="delete it yourself"):
        tart_golden.build_golden("HEAD", tmp_path / "ev", name="ava-golden", repo=repo, tart=exists)

    crowded = Listing(
        [
            {"Name": "a", "Running": True, "Source": "local"},
            {"Name": "b", "Running": True, "Source": "local"},
        ]
    )
    with pytest.raises(RunFailedError, match="at most 2"):
        tart_golden.build_golden(
            "HEAD", tmp_path / "ev", name="ava-golden", repo=repo, tart=crowded
        )


def test_the_guest_scripts_pin_the_pieces_the_experiment_found_necessary() -> None:
    # The helper is restarted through launchd after the toggles, not left running.
    assert "launchctl kickstart -k" in tart_golden.RESTART_HELPER
    # The golden image carries no source: each run fetches its own commit.
    assert tart_golden.FINALIZE.splitlines()[-1] == 'rm -rf "$HOME/.ava/source"'
    # The verification reads the system TCC database read-only and never writes it.
    assert "sqlite3" in tart_golden.VERIFY and "-readonly" in tart_golden.VERIFY
    assert not re.search(r"\b(insert|update|delete)\b", tart_golden.VERIFY, re.I)


# --------------------------------------------------------------------------- the verdict


def test_a_fully_granted_helper_passes() -> None:
    assert tart_golden.judge_grant(GRANTED) == []


_DROP = object()


def _edit(record: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Change one field of a granted record: set `path` to `value`, or remove it."""
    *parents, last = path
    target: Any = record
    for key in parents:
        target = target[key]
    if value is _DROP:
        del target[last]
    else:
        target[last] = value


@pytest.mark.parametrize(
    ("path", "value", "complaint"),
    [
        (("ping", "preflight_screen"), False, "preflight_screen"),
        (("ping", "ax_trusted"), False, "ax_trusted"),
        (("tcc", "kTCCServiceScreenCapture"), [0, f"FADE{SHA1}"], "ScreenCapture"),
        (("tcc", "kTCCServiceAccessibility"), _DROP, "Accessibility"),
        (("tcc", "kTCCServiceAccessibility"), [2, "FADE0C00"], "requirement"),
        (("identity",), "no identities", "not in the keychain"),
        (("signature",), ["CDHash=abc"], "designated requirement"),
        (("capture",), {"distinct": 3, "top_share": 0.99}, "uniform"),
        (("capture",), {"error": "RuntimeError('no display')"}, "no capture"),
    ],
)
def test_anything_short_of_the_full_grant_is_a_problem(
    path: tuple[str, ...], value: Any, complaint: str
) -> None:
    record = copy.deepcopy(GRANTED)
    _edit(record, path, value)

    problems = tart_golden.judge_grant(record)

    assert problems and any(complaint in problem for problem in problems)
