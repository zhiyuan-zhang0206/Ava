"""Cleanup consumes only the exact resources retained by the original execution."""

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agent.graph.exec._output_pipe import ExecOutputPipe
from agent.graph.exec._process import ExecTeardownError, TeardownFailure
from agent.graph.exec._stream import StreamingTextIO
from agent.graph.exec._subprocess import _finish_request_evidence, _retain_late_reader_completion
from base.native_process.turn_identity import HostedTurnResources
from tests.fixtures.pin_agent import hosted_resources as hosted_resources


def _files(directory: Path) -> tuple[Path, Path]:
    request, result = directory / "request.json", directory / "result.json"
    request.write_text("request")
    result.write_text("result")
    return request, result


def _closed_output_pipe() -> ExecOutputPipe:
    source, writer = os.pipe()
    os.close(writer)
    # The output owner consumes only Popen's stdout handle. Use an actual pipe
    # at EOF without launching a process unrelated to the resource-CAS proof.
    proc = cast("subprocess.Popen[bytes]", SimpleNamespace(stdout=os.fdopen(source, "rb")))
    reader = ExecOutputPipe(proc, StreamingTextIO(max_chars=64))
    reader.finish_now(0)
    assert reader.closed
    return reader


def test_failed_cleanup_keeps_primary_resource_and_wire_evidence(tmp_path: Path) -> None:
    request, result = _files(tmp_path)
    resources = HostedTurnResources()
    domain = object()
    resources.unresolved[request] = domain
    _finish_request_evidence(request, result, domain, settled=False, resources=resources)
    assert resources.unresolved[request] is domain
    assert request.exists() and result.exists()
    assert not resources.changed.is_set()
    _finish_request_evidence(request, result, object(), settled=True, resources=resources)
    assert resources.unresolved[request] is domain
    assert request.exists() and result.exists()
    _finish_request_evidence(request, result, domain, settled=True, resources=resources)
    assert not resources.unresolved
    assert not request.exists() and not result.exists()


async def test_late_reader_cannot_clean_replacement_domain(
    tmp_path: Path, hosted_resources: HostedTurnResources
) -> None:
    request, result = _files(tmp_path)
    resources = hosted_resources
    original, replacement = object(), object()
    resources.unresolved[request] = original
    failure = ExecTeardownError((TeardownFailure("reader_join", TimeoutError("join")),))
    reader = _closed_output_pipe()
    _retain_late_reader_completion(failure, request, result, reader, resources=resources)
    tasks = set(resources.completions)
    resources.unresolved[request] = replacement
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert resources.unresolved[request] is replacement
    assert request.exists() and result.exists()
    assert not resources.completions


async def test_secondary_teardown_failure_cannot_certify_cleanup(tmp_path: Path) -> None:
    request, result = _files(tmp_path)
    resources = HostedTurnResources()
    domain = object()
    resources.unresolved[request] = domain
    failure = ExecTeardownError(
        (
            TeardownFailure("reader_join", TimeoutError("join")),
            TeardownFailure("domain_close", RuntimeError("close")),
        )
    )
    _retain_late_reader_completion(
        failure, request, result, _closed_output_pipe(), resources=resources
    )
    assert not resources.completions
    assert resources.unresolved[request] is domain
    assert request.exists() and result.exists()
    assert [item.stage for item in failure.failures] == ["reader_join", "domain_close"]


async def test_actual_owner_completion_unknown_reaches_original_service() -> None:
    from agent.graph.exec._owned_run import _OwnedRun
    from base.agents.incarnation.exec_owner_protocol import OwnerClosed
    from base.native_process.turn_identity import HostedServiceResources

    service = HostedServiceResources()
    scope = await service.turn()
    request, domain = Path("original-owner-request"), object()
    scope.unresolved[request] = domain
    error = ValueError("unknown close receipt failure")

    async def failed_receipt() -> OwnerClosed:
        raise error

    completion = asyncio.create_task(failed_receipt())
    owned = object.__new__(_OwnedRun)
    owned.registration = None
    owned.attached = True
    owned.attached_completion = lambda: completion
    service = scope.require_service()
    service.complete_later(scope, owned.finish_owner(), name="actual-owner-result")
    while scope.completions:
        await asyncio.sleep(0)
    assert service.failures == [(scope, error)]
    assert scope.unresolved[request] is domain
    with pytest.raises(ValueError) as observed:
        await service.aclose()
    assert observed.value is error
