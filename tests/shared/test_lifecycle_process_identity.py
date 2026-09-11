"""Lifecycle target identity: a whole-second create_time move is not an exit."""

from __future__ import annotations

import os

import psutil

from shared.lifecycle_process_identity import target_process_ended


def _payload(offset: float) -> dict[str, object]:
    return {
        "target_process_identity": {
            "machine": "machine",
            "pid": os.getpid(),
            "create_time": psutil.Process().create_time() + offset,
            "starttime": None,
        }
    }


def test_live_process_within_the_tolerance_is_not_ended() -> None:
    assert not target_process_ended(_payload(-1.0), "machine")
    assert not target_process_ended(_payload(1.0), "machine")


def test_birth_beyond_the_tolerance_is_an_end_or_reuse() -> None:
    assert target_process_ended(_payload(-3.0), "machine")
    assert target_process_ended(_payload(60.0), "machine")
