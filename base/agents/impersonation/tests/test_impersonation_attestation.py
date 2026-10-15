from typing import Any
from uuid import uuid4

import pytest

from base.agents.impersonation import _store as store
from base.agents.impersonation.tests.impersonation_support import recorded_tree


def _lease(tree: dict[str, Any]) -> dict[str, Any]:
    return {"process_metadata": tree, "machine": "attestation-unit"}


def test_authenticate_requires_same_machine() -> None:
    lease = _lease(recorded_tree())
    store.authenticate(lease, local_machine="attestation-unit")
    lease["machine"] += "-other"
    with pytest.raises(store.ImpersonationError, match="own machine"):
        store.authenticate(lease, local_machine="attestation-unit")


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_validate_relay_spec_requires_thread_id_for_host_relays(provider: str) -> None:
    store.validate_relay_spec(provider, str(uuid4()), None)
    with pytest.raises(ValueError, match="thread id"):
        store.validate_relay_spec(provider, None, None)


def test_validate_relay_spec_rejects_codex_remote_on_antigravity() -> None:
    with pytest.raises(ValueError, match="does not use --codex-remote"):
        store.validate_relay_spec("antigravity", str(uuid4()), "unix:///tmp/codex.sock")


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_validate_relay_spec_rejects_thread_id_for_session_relays(provider: str) -> None:
    store.validate_relay_spec(provider, None, None)
    with pytest.raises(ValueError, match="routes to its owner"):
        store.validate_relay_spec(provider, str(uuid4()), None)
