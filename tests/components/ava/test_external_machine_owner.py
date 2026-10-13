"""Attachments keep the explicit machine owner's cache and borrow boundaries."""

import os
from typing import Any
from unittest.mock import patch

import ava
from ava import impersonation
from ava.sdk_surface import process_context
from base.cluster.machine import MachineIdentity
from tests.factories.external_attachment import attached_runtime as attached_runtime


def test_attachment_borrows_same_machine_without_resetting_prior_cache(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    prior = ava.context
    machine = prior.require_machine()
    assert machine.name() == "local-runner"
    # The attachment captures its own process input, not the Settings singleton.
    with (
        patch.dict(os.environ, {"AVA_MACHINE_NAME": "unrelated-process-change"}),
        impersonation.attach("lease"),
    ):
        assert ava.context.require_machine() is machine
        assert ava.context.require_machine().name() == "local-runner"
    assert ava.context is prior
    assert prior.require_machine() is machine
    assert machine.name() == "local-runner"


def test_independent_attachments_own_fresh_machine_caches(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
) -> None:
    lease, _, _ = attached_runtime
    prior = process_context.unbind_context()
    owners: list[MachineIdentity] = []
    try:
        for name in ("first-attachment", "second-attachment"):
            lease["machine"] = name
            # Each independent root captures this input before lease validation.
            with patch.dict(os.environ, {"AVA_MACHINE_NAME": name}), impersonation.attach("lease"):
                machine = ava.context.require_machine()
                assert machine.name() == name
                assert all(machine is not previous for previous in owners)
                owners.append(machine)
            assert getattr(ava, "context", None) is None
        assert owners[0].name() == "first-attachment"
        assert owners[1].name() == "second-attachment"
    finally:
        if prior is not None:
            process_context.bind_context(prior)
