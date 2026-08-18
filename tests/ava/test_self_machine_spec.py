"""ava.self.MACHINE_SPEC = (name, description) from local config; "" when unset."""

from __future__ import annotations

import ava.self as self_mod


def test_machine_spec_tuple_with_description() -> None:
    from shared.machine import reset_identity, set_identity

    set_identity(name="test-host", description="voice IO + browser")
    try:
        assert ava_self_machine_spec() == ("test-host", "voice IO + browser")
    finally:
        reset_identity()


def test_machine_spec_tuple_empty_when_no_description() -> None:
    from shared.machine import reset_identity, set_identity

    set_identity(name="wsl", description=None)
    try:
        assert ava_self_machine_spec() == ("wsl", "")
    finally:
        reset_identity()


def ava_self_machine_spec():
    # Re-resolved through module __getattr__ each call (not cached at import).
    return self_mod.MACHINE_SPEC
