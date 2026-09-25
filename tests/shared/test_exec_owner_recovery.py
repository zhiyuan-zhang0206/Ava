"""Exec-owner recovery compares exact captured native birth, not a nearby PID occupant."""

from __future__ import annotations

import os

import psutil

from shared.exec_owner_recovery import process_ended
from shared.incarnation_resources import ResourceProcess
from shared.proc_tree import stable_create_time


def _self_identity(offset: float) -> ResourceProcess:
    return ResourceProcess(pid=os.getpid(), birth=stable_create_time(psutil.Process()) + offset)


def test_exact_native_process_has_not_ended() -> None:
    assert not process_ended(_self_identity(0))


def test_nearby_birth_is_positive_evidence_of_reuse() -> None:
    assert process_ended(_self_identity(-0.0001))
    assert process_ended(_self_identity(0.0001))
