"""The helper factory for tests that must never reach a permissions helper.

A code path that needs the helper takes a `HelperFactory`; a test that does not mean to
exercise that path passes this one, so a stray reach fails on the spot instead of dialling the
real helper socket of whatever home the test process runs under."""

from pathlib import Path

from base.host.permissions_helper.client import PermissionsHelperClient

__all__ = ["refuse_connect", "refuse_helper"]


def refuse_helper() -> PermissionsHelperClient:
    """Fail the test that reached for a helper it did not inject."""
    raise AssertionError(
        "this test reached the permissions helper without injecting one: pass a helper factory "
        "that returns a scripted PermissionsHelperClient subclass instead of refuse_helper"
    )


def refuse_connect(socket_path: Path) -> PermissionsHelperClient:
    """Fail the test that dialled a helper socket it did not inject a client for."""
    raise AssertionError(
        f"this test dialled the permissions helper at {socket_path} without injecting a client: "
        "pass a connect function that returns a scripted PermissionsHelperClient subclass"
    )
