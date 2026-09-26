"""PITR identity pairs read through the stable start-time key.

Every PITR capture/compare pair — owner evidence, the sandbox postmaster
identity, the recorded worker handshake value — records a live process's start
time and re-reads it later. On macOS psutil's public create_time() re-derives
from the wall clock with a whole-second boot-time correction, so a pair spanning
two import epochs (or any clock step) disagreed by up to a second: far past the
0.01s window these sites used before task #3157. The stable key the sites now
read is epoch-invariant, so a recorded identity keeps claiming its process.
"""

from __future__ import annotations

import importlib
import os
import sys

import psutil
import pytest

from services.pitr import restore_postgres, restore_proof
from services.pitr.restore_postgres import SandboxPostgresIdentity
from services.pitr.worker_process import NativeProcess

_macos_correction = pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)

_EPOCH_SHIFTS = (60.0, 3600.0)


def _psosx():
    return importlib.import_module("psutil._psosx")


@_macos_correction
@pytest.mark.parametrize("seconds", _EPOCH_SHIFTS)
def test_owner_evidence_identity_survives_an_epoch_shift(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """The recorded owner identity keeps matching its process after a clock step.

    Not vacuous: under the same shift the public reading moves by the whole
    correction, which is what these sites compared before the stable key.
    """
    psosx = _psosx()
    base = psosx.INIT_BOOT_TIME
    process = psutil.Process()
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + seconds)
    recorded = NativeProcess.capture(process)  # what the worker's owner receipt writes
    assert abs(psutil.Process().create_time() - recorded.process.birth) > 2.0
    # Still inside the shifted epoch: catches a reader that went back to the
    # public value (the receipt and the controller's capture would then disagree
    # by the whole correction). Then the crossing itself, after the epoch returns.
    assert recorded.same_birth(NativeProcess.capture(psutil.Process()))
    assert recorded.live() is not None
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
    assert recorded.same_birth(NativeProcess.capture(psutil.Process()))
    assert recorded.live() is not None


@_macos_correction
@pytest.mark.parametrize("seconds", _EPOCH_SHIFTS)
def test_sandbox_identity_survives_an_epoch_shift(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """The sandbox postmaster identity pair spans epochs without breaking."""
    psosx = _psosx()
    base = psosx.INIT_BOOT_TIME
    process = psutil.Process()
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + seconds)
    identity = SandboxPostgresIdentity(
        NativeProcess.capture(process), os.getpgid(process.pid), os.getsid(process.pid), "/data"
    )
    assert restore_postgres._matching_sandbox(identity) is not None
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
    assert restore_postgres._matching_sandbox(identity) is not None


@_macos_correction
@pytest.mark.parametrize("seconds", _EPOCH_SHIFTS)
def test_restore_owner_identity_survives_an_epoch_shift(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """The restore owner evidence (task #3157 sites) is epoch-invariant too."""
    psosx = _psosx()
    base = psosx.INIT_BOOT_TIME
    process = psutil.Process()
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + seconds)
    recorded = NativeProcess.capture(process)  # what the owner-evidence writes store
    assert restore_proof._matching_process(recorded) is not None
    monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
    assert restore_proof._matching_process(recorded) is not None
