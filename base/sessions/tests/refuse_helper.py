from pathlib import Path

from base.host.permissions_helper.client import PermissionsHelperClient

__all__ = ["refuse_connect", "refuse_helper"]


def refuse_helper() -> PermissionsHelperClient:
    raise AssertionError(
        "this test reached the permissions helper without injecting one: pass a helper factory "
        "that returns a scripted PermissionsHelperClient subclass instead of refuse_helper"
    )


def refuse_connect(socket_path: Path) -> PermissionsHelperClient:
    raise AssertionError(
        f"this test dialled the permissions helper at {socket_path} without injecting a client: "
        "pass a connect function that returns a scripted PermissionsHelperClient subclass"
    )
