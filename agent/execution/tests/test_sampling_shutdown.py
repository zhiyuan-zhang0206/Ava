"""Real SDK refresh attempts finish before the child's outcome is materialized."""

import threading
from types import SimpleNamespace

import pytest
from loguru import logger

from agent.execution import child
from agent.graph.exec.protocol import ResultPayload
from ava.sdk_surface import install
from base.agents.sdk import call_policy, telemetry
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkNamespace
from tests.fixtures.pin_agent import pin_agent


def _assert_outcome(
    payload: ResultPayload,
    outcome: str,
    sampling: call_policy.SamplingPolicyOwner,
    body_error: ValueError,
    reports: list[str],
) -> None:
    if outcome == "blocked":
        assert payload.kind == "done"
        assert sampling.worker is not None and sampling.worker.thread.is_alive()
        assert any("refresh unfinished" in report for report in reports)
    elif outcome == "body_failure":
        assert payload.kind == "crashed" and payload.exc_type == "ValueError"
        assert payload.full_traceback is not None
        assert "sampling reader bug" in payload.full_traceback
        assert body_error.__notes__ and "shutdown also failed" in body_error.__notes__[0]
    else:
        assert payload.kind == "crashed" and payload.exc_type == "TypeError"
        assert payload.exc_msg == "sampling reader bug"


@pytest.mark.parametrize("outcome", ["blocked", "body_failure", "refresh_failure"])
def test_run_code_collects_sampling_at_its_result_boundary(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    refresh_error, body_error = TypeError("sampling reader bug"), ValueError("agent body failed")

    def read() -> call_policy.SamplingPolicy:
        entered.set()
        assert release.wait(5)
        if outcome != "blocked":
            raise refresh_error
        return call_policy.SamplingPolicy()

    sampling = call_policy.SamplingPolicyOwner(reader=read)

    def body() -> None:
        assert entered.wait(2)
        if outcome != "blocked":
            release.set()
            assert sampling.worker is not None and sampling.worker.completed.wait(2)
        if outcome == "body_failure":
            raise body_error

    def emit(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(telemetry, "emit", emit)
    real_stop = sampling.stop
    monkeypatch.setattr(sampling, "stop", lambda: real_stop(0.02))
    install.uninstall()
    install.install(
        ExtensionRegistry(
            (
                (
                    "probe",
                    PluginContributions(
                        sdk_namespaces=(SdkNamespace("probe", SimpleNamespace(body=body)),)
                    ),
                ),
            )
        ),
        sampling=sampling,
    )
    pin_agent(37)
    payload = ResultPayload(kind="done")
    reports: list[str] = []
    sink = logger.add(lambda message: reports.append(str(message)))
    try:
        child._run_code("import ava; ava.probe.body()", payload)
        assert payload.code_reached
        _assert_outcome(payload, outcome, sampling, body_error, reports)
        assert sampling.error is None or sampling.error[0] is refresh_error
    finally:
        release.set()
        if sampling.worker is not None:
            assert sampling.worker.completed.wait(2)
            assert sampling.worker.stop(2)
        logger.remove(sink)
        install.uninstall()
