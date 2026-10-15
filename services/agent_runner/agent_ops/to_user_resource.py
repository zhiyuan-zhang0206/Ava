import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.to_user import ToUserReadRequest, read_resource
from ops.to_user import owned_workspace
from services.agent_runner.agent_ops import maintenance


def resource_response(body: bytes, *, pool: ConnectionPool, machine: str) -> tuple[int, bytes, str]:

    try:
        request = ToUserReadRequest.model_validate_json(body)
        workspace = owned_workspace(pool, request.agent_id, machine)
        content = read_resource(workspace, request.references)
    except FileNotFoundError:
        return 404, b"Referenced file is no longer available", "text/plain"
    except (ValueError, OSError, ValidationError):
        return 422, b"Referenced file could not be read", "text/plain"
    return 200, content, "application/octet-stream"


async def resource_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: maintenance.RequestTokens,
    workers: maintenance.WorkerFutures,
) -> tuple[int, bytes, str]:

    async with dispatch_sem:
        try:
            with maintenance.admission("to_user_resource", requests=requests):
                future = asyncio.get_running_loop().run_in_executor(
                    executor,
                    functools.partial(resource_response, body, pool=pool, machine=machine),
                )
                maintenance.track_worker(future, workers=workers)
                return await asyncio.shield(future)
        except RuntimeError:
            return 503, b"The agent's machine is stopping", "text/plain"
