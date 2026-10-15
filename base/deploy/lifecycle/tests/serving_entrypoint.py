from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def inject_fixture_gate(gate: Path) -> None:
    from base.deploy.lifecycle import start_serving

    def fixture_is_serving() -> bool:
        return gate.is_file()

    @contextmanager
    def fixture_recovery() -> Generator[bool]:
        yield fixture_is_serving()

    start_serving.is_serving = fixture_is_serving
    start_serving.recovery_permitted = fixture_recovery
