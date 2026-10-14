"""Direct-process serving injection for end-to-end fixtures and the start-serving tests.

A test-only injection that replaces the native birth gate's two product functions with a
file-backed fixture. The e2e harness calls it from `python -m tests.e2e.process_support GATE
MODULE ...` before running the target module; the start-serving tests call it in process. It lives
with the tests of `base.deploy.lifecycle`, the code it replaces parts of.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def inject_fixture_gate(gate: Path) -> None:
    """Serve while `gate` exists, deliberately excluding root custody.

    This test-only injection is explicitly selected by the e2e conftest; no environment flag or
    product entry point can bypass the real native birth gate.
    """
    from base.deploy.lifecycle import start_serving

    def fixture_is_serving() -> bool:
        return gate.is_file()

    @contextmanager
    def fixture_recovery() -> Generator[bool]:
        yield fixture_is_serving()

    start_serving.is_serving = fixture_is_serving
    start_serving.recovery_permitted = fixture_recovery
