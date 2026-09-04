"""CANARY probe: deterministic failure to verify Trunk queue blocking.

Deliberately failing test used once to prove the flipped Trunk upload step
uploads a failing suite and the merge queue refuses the PR. Never lands on
main: the PR is closed without merge. Remove this file when done.
"""


def test_canary_trunk_blocking_probe() -> None:
    raise AssertionError("CANARY: deterministic failure to verify Trunk queue blocking")
