"""Child process for the sampling-owner atexit test: a refresh defect surfaces at shutdown."""

import ava  # noqa: F401  # pyright: ignore[reportUnusedImport] (importing the SDK is under test)
from ava.sdk_surface import install
from ava.sdk_surface.installation import installed
from base.agents.sdk import call_policy
from base.packages.plugins.extensions import ExtensionRegistry

install.install(ExtensionRegistry(()))
original = ValueError("ordinary SDK refresh defect")


def read() -> object:
    raise original


call_policy.read_policy = read  # pyright: ignore[reportAttributeAccessIssue]
installation = installed()
assert installation is not None
owner = installation.sampling
try:
    owner.read()
except ValueError as observed:
    assert observed is original
assert owner.worker is not None and owner.worker.completed.wait(2)
assert owner.error is not None and owner.error[0] is original
