"""ava.self.MACHINE_SPEC = MachineSpec(name, description) from local config; "" when unset."""

from __future__ import annotations

import ava.self as self_mod


def test_machine_spec_tuple_with_description() -> None:
    from base.cluster.machine import reset_identity, set_identity

    set_identity(name="test-host", description="voice IO + browser")
    try:
        spec = ava_self_machine_spec()
        assert spec == ("test-host", "voice IO + browser")
        assert isinstance(spec, self_mod.MachineSpec)
        assert (spec.name, spec.description) == ("test-host", "voice IO + browser")
        name, description = spec
        assert name == "test-host" and description == "voice IO + browser"
    finally:
        reset_identity()


def test_machine_spec_tuple_empty_when_no_description() -> None:
    from base.cluster.machine import reset_identity, set_identity

    set_identity(name="runner-a", description=None)
    try:
        assert ava_self_machine_spec() == ("runner-a", "")
    finally:
        reset_identity()


def ava_self_machine_spec():
    # Re-resolved through module __getattr__ each call (not cached at import).
    return self_mod.MACHINE_SPEC
