"""Exec-owner recovery: a whole-second create_time move is not death evidence."""

from __future__ import annotations

import os

import psutil

from shared.exec_owner_recovery import process_ended
from shared.incarnation_resources import ResourceProcess


def _self_identity(offset: float) -> ResourceProcess:
    # The recorded birth is re-derived from the wall clock and moves by whole
    # seconds while the process stays alive (macOS psutil).
    return ResourceProcess(pid=os.getpid(), birth=psutil.Process().create_time() + offset)


def test_live_process_within_the_tolerance_has_not_ended() -> None:
    assert not process_ended(_self_identity(-1.0))
    assert not process_ended(_self_identity(1.0))


def test_birth_beyond_the_tolerance_is_positive_evidence_of_an_end() -> None:
    assert process_ended(_self_identity(-3.0))
    assert process_ended(_self_identity(60.0))
