"""Real service lifespan for tests that invoke a hosted turn's private boundary."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources


@asynccontextmanager
async def hosted_scope(
    *, expected_error: type[BaseException] | None = None
) -> AsyncGenerator[HostedTurnResources, None]:
    """Join actual retained tasks and verify the one explicitly expected original error."""
    service = HostedServiceResources()
    scope = await service.turn()
    primary: BaseException | None = None
    try:
        yield scope
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if expected_error is None:
                await service.aclose()
            else:
                with pytest.raises(expected_error) as joined:
                    await service.aclose()
                assert len(service.failures) == 1
                original_scope, original_error = service.failures[0]
                assert original_scope is scope
                assert joined.value is original_error
            assert service.joined
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"hosted test service join failed: {cleanup!r}")
