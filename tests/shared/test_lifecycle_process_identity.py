"""Lifecycle target identity requires the exact stable native birth."""

from __future__ import annotations

import os

import psutil

from shared.lifecycle_process_identity import target_process_ended
from shared.proc_tree import stable_create_time


def _payload(offset: float) -> dict[str, object]:
    return {
        "target_process_identity": {
            "machine": "machine",
            "pid": os.getpid(),
            "create_time": stable_create_time(psutil.Process()) + offset,
            "starttime": None,
        }
    }


def test_exact_native_process_is_not_ended() -> None:
    assert not target_process_ended(_payload(0), "machine")


def test_nearby_birth_is_reuse() -> None:
    assert target_process_ended(_payload(-0.0001), "machine")
    assert target_process_ended(_payload(0.0001), "machine")
