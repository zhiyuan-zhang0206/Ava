"""Cleanup consumes only the exact resources retained by the original execution."""

import asyncio
import threading
from pathlib import Path

from agent.graph.exec._process import ExecTeardownError, TeardownFailure
from agent.graph.exec._subprocess import _finish_request_evidence, _retain_late_reader_completion
from base.native_process.turn_identity import HostedTurnResources


def _files(directory: Path) -> tuple[Path, Path]:
    request, result = directory / "request.json", directory / "result.json"
    request.write_text("request")
    result.write_text("result")
    return request, result


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


async def test_late_reader_cannot_clean_replacement_domain(tmp_path: Path) -> None:
    request, result = _files(tmp_path)
    resources = HostedTurnResources()
    original, replacement = object(), object()
    resources.unresolved[request] = original
    failure = ExecTeardownError((TeardownFailure("reader_join", TimeoutError("join")),))
    reader = threading.Thread()  # Already finished: no new join owner or worker is created.
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
        failure, request, result, threading.Thread(), resources=resources
    )
    assert not resources.completions
    assert resources.unresolved[request] is domain
    assert request.exists() and result.exists()
    assert [item.stage for item in failure.failures] == ["reader_join", "domain_close"]
